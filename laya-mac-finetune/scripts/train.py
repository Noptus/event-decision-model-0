#!/usr/bin/env python3
"""Conservative single-process Laya fine-tuning for Apple Silicon."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import random
import shutil
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import torch
from laya.calibrate import fit_abstention_thresholds, fit_temperature_map
from laya.common import proper_reward, temp_bucket
from safetensors.torch import load_file, save_file

from _shared import (
    BASE_MODEL_DIR,
    CONFIG_PATH,
    DATA_DIR,
    OUTPUT_DIR,
    ROUTES,
    ROUTE_CONTRACT_PATH,
    atomic_write_json,
    build_training_item,
    clear_device_cache,
    collate_training_items,
    freeze_model,
    load_config,
    load_trainable_model,
    mps_memory,
    parameter_counts,
    question_contract_hash,
    read_jsonl,
    require_free_disk,
    select_device,
    set_seed,
    update_manifest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "experiment"), default="smoke")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"))
    parser.add_argument("--max-steps", type=int)
    return parser.parse_args()


@contextmanager
def exclusive_training():
    """Prevent concurrent cache writers and checkpoint replacements."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUTPUT_DIR / ".training.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            owner = handle.read().strip() or "unknown"
            raise SystemExit(f"Another training run is active in this project (PID {owner}); wait for it to finish.")
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def validate_exposure_plan(
    mode: str,
    max_steps: int,
    micro_batch: int,
    gradient_accumulation: int,
    training_examples: int,
    minimum_epochs: int,
) -> tuple[int, int]:
    planned = max_steps * micro_batch * gradient_accumulation
    required = minimum_epochs * training_examples
    if mode == "experiment" and planned < required:
        raise ValueError(
            "planned optimizer steps do not cover the required full passes: "
            f"{max_steps} * {micro_batch} * {gradient_accumulation} = {planned} "
            f"examples, need at least {required}"
        )
    return planned, required


def balanced_subset(records: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    buckets = {route: [] for route in ROUTES}
    for record in records:
        buckets[record["gold"]["route"]["label"]].append(record)
    selected: list[dict[str, Any]] = []
    while len(selected) < size:
        progressed = False
        for route in ROUTES:
            if buckets[route] and len(selected) < size:
                selected.append(buckets[route].pop(0))
                progressed = True
        if not progressed:
            break
    return selected


def move_batch(batch: dict[str, torch.Tensor], device: torch.device) -> dict[str, torch.Tensor]:
    return {key: value.to(device) for key, value in batch.items()}


def set_training_mode(model, freeze_strategy: str) -> None:
    model.train()
    if freeze_strategy == "head_only":
        # The backbone is frozen: disabling its dropout makes repeated states deterministic and
        # does not prevent gradients through the native decision head.
        model.encoder.eval()


def forward_from_hidden(model, hidden_states: torch.Tensor, batch: dict[str, torch.Tensor]):
    """Run the unchanged native Laya decision head over a frozen encoder representation."""
    hidden = hidden_states + model.type_emb(batch["qtype"])[:, None, :]
    if model.head is not None:
        padding_mask = ~batch["attention_mask"].bool()
        for layer in model.head.layers:
            hidden = layer(hidden, src_key_padding_mask=padding_mask)
    index = batch["marker_pos"].clamp(min=0)[:, :, None].expand(-1, -1, hidden.size(-1))
    markers = torch.gather(hidden, 1, index)
    logits = model.scorer(markers).squeeze(-1).float()
    logits = logits.masked_fill(~batch["marker_mask"], -1e4)
    probabilities = torch.softmax(logits.detach(), -1)
    option_count = batch["marker_mask"].sum(-1).clamp(min=2).float()
    top_two = probabilities.topk(2, -1).values
    entropy = -(
        probabilities * torch.log(probabilities.clamp_min(1e-9))
    ).sum(-1) / torch.log(option_count)
    features = torch.stack(
        [top_two[:, 0], top_two[:, 0] - top_two[:, 1], entropy, option_count / 255.0], -1
    )
    activation = model.act_head(torch.cat([hidden[:, 0].float(), features], -1))
    return logits, activation


@lru_cache(maxsize=2)
def encoder_weights_hash(path: str, size: int, modified_ns: int) -> str:
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def encoder_cache_key(items: list[dict[str, Any]], model_config: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256("\n".join(item["id"] for item in items).encode()).hexdigest()
    inputs = hashlib.sha256()
    for item in items:
        inputs.update(json.dumps(item["ids"], separators=(",", ":")).encode())
        inputs.update(b"\n")
    weights = BASE_MODEL_DIR / "model.safetensors"
    stat = weights.stat()
    return {
        "item_ids_sha256": digest,
        "input_tokens_sha256": inputs.hexdigest(),
        "encoder_weights_sha256": encoder_weights_hash(str(weights), stat.st_size, stat.st_mtime_ns),
        "count": len(items),
        "max_len": int(model_config["max_len"]),
        "hidden_size": int(model_config.get("hidden_size", 0)),
        "encoder": model_config.get("encoder"),
        "dtype": "float16",
        "contains_labels": False,
    }


def build_encoder_cache(
    model,
    items: list[dict[str, Any]],
    tokenizer,
    model_config: dict[str, Any],
    device: torch.device,
    cache_name: str,
    batch_size: int,
):
    cache_dir = OUTPUT_DIR / "encoder-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_path = cache_dir / f"{cache_name}.npy"
    metadata_path = cache_dir / f"{cache_name}.json"
    partial_path = cache_dir / f".{cache_name}.partial.npy"
    progress_path = cache_dir / f".{cache_name}.partial.json"
    key = encoder_cache_key(items, model_config)
    key["hidden_size"] = int(model.encoder.config.hidden_size)
    shape = (len(items), int(model_config["max_len"]), int(model.encoder.config.hidden_size))
    if data_path.exists() and metadata_path.exists():
        with metadata_path.open(encoding="utf-8") as handle:
            cached_key = json.load(handle)
        if cached_key == key:
            print(f"Using frozen encoder cache: {data_path}")
            return np.load(data_path, mmap_mode="r"), 0.0

    start_row = 0
    cache = None
    if partial_path.exists() and progress_path.exists():
        with progress_path.open(encoding="utf-8") as handle:
            progress = json.load(handle)
        if progress.get("cache_key") == key and tuple(progress.get("shape", ())) == shape:
            start_row = int(progress.get("completed_rows", 0))
            if 0 <= start_row <= len(items):
                cache = np.lib.format.open_memmap(partial_path, mode="r+")
                print(f"Resuming frozen encoder cache at {start_row}/{len(items)} rows")
    if cache is None:
        partial_path.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
        cache = np.lib.format.open_memmap(partial_path, mode="w+", dtype=np.float16, shape=shape)
        atomic_write_json(
            progress_path,
            {"cache_key": key, "shape": list(shape), "completed_rows": 0},
        )

    model.encoder.eval()
    started = time.perf_counter()
    with torch.no_grad():
        for start in range(start_row, len(items), batch_size):
            chunk = items[start : start + batch_size]
            batch = move_batch(
                collate_training_items(chunk, tokenizer.pad_token_id, int(model_config["max_len"])),
                device,
            )
            hidden = model.encoder(
                input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]
            ).last_hidden_state
            completed_rows = start + len(chunk)
            cache[start:completed_rows] = hidden.float().cpu().numpy().astype(np.float16)
            if completed_rows % (batch_size * 10) == 0 or completed_rows == len(items):
                cache.flush()
                atomic_write_json(
                    progress_path,
                    {"cache_key": key, "shape": list(shape), "completed_rows": completed_rows},
                )
            if (start // batch_size + 1) % 100 == 0:
                print(f"cached {completed_rows}/{len(items)} frozen encoder rows")
    cache.flush()
    del cache
    os.replace(partial_path, data_path)
    atomic_write_json(metadata_path, key)
    progress_path.unlink(missing_ok=True)
    elapsed = time.perf_counter() - started
    print(f"Built label-free frozen encoder cache {data_path} in {elapsed:.1f}s")
    return np.load(data_path, mmap_mode="r"), elapsed


def forward_loss(
    model,
    batch: dict[str, torch.Tensor],
    freeze_strategy: str,
    sigma: float,
    group_size: int,
    hidden_states: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, float]]:
    if hidden_states is None:
        logits, activation = model(
            batch["input_ids"],
            batch["attention_mask"],
            batch["marker_pos"],
            batch["marker_mask"],
            batch["qtype"],
            detach_encoder=freeze_strategy == "head_only",
        )
    else:
        logits, activation = forward_from_hidden(model, hidden_states, batch)
    logits = logits.float()
    mask = batch["marker_mask"]
    target = batch["target"]
    option_count = mask.sum(-1, keepdim=True).float()

    epsilon = torch.randn((group_size,) + logits.shape, device=logits.device) * sigma * mask
    epsilon = (epsilon - epsilon.sum(-1, keepdim=True) / option_count) * mask
    noisy_logits = logits.detach().unsqueeze(0) + epsilon
    sampled_probabilities = torch.softmax(noisy_logits.masked_fill(~mask, -1e4), -1)
    with torch.no_grad():
        reward = proper_reward(
            sampled_probabilities,
            target.unsqueeze(0),
            batch["qtype"],
            mask,
            w_sph=0.75,
            w_rps=1.0,
        )
        advantage = reward - reward.mean(0, keepdim=True)
        advantage = advantage / (advantage.std(unbiased=False) + 1e-6)

    log_probability = -(
        ((noisy_logits - logits.unsqueeze(0)) ** 2) * mask
    ).sum(-1) / (2 * sigma**2)
    loss_rlcd = -(advantage * log_probability).mean()
    loss_ce = -(
        target * torch.log_softmax(logits.masked_fill(~mask, -1e4), -1)
    ).sum(-1).mean()
    loss = loss_rlcd + loss_ce + 0.0 * activation.sum()
    return loss, {
        "loss": float(loss.detach().cpu()),
        "loss_rlcd": float(loss_rlcd.detach().cpu()),
        "loss_ce": float(loss_ce.detach().cpu()),
        "reward": float(reward.mean().detach().cpu()),
    }


@torch.no_grad()
def evaluate_model(
    model,
    items: list[dict[str, Any]],
    tokenizer,
    max_length: int,
    batch_size: int,
    device: torch.device,
    freeze_strategy: str,
    collect_records: bool = False,
    feature_cache=None,
) -> tuple[dict[str, float], list[tuple[int, Any, Any, int]]]:
    model.eval()
    losses: list[float] = []
    correct = 0
    total = 0
    records = []
    for start in range(0, len(items), batch_size):
        chunk = items[start : start + batch_size]
        batch = move_batch(collate_training_items(chunk, tokenizer.pad_token_id, max_length), device)
        if feature_cache is None:
            logits, _ = model(
                batch["input_ids"],
                batch["attention_mask"],
                batch["marker_pos"],
                batch["marker_mask"],
                batch["qtype"],
                detach_encoder=freeze_strategy == "head_only",
            )
        else:
            hidden = torch.from_numpy(np.array(feature_cache[start : start + len(chunk)], copy=True)).to(
                device=device, dtype=torch.float32
            )
            logits, _ = forward_from_hidden(model, hidden, batch)
        logits = logits.float()
        masked = logits.masked_fill(~batch["marker_mask"], -1e4)
        per_item_loss = -(batch["target"] * torch.log_softmax(masked, -1)).sum(-1)
        losses.extend(float(value) for value in per_item_loss.cpu())
        predictions = masked.argmax(-1)
        correct += int((predictions == batch["labels"]).sum().cpu())
        total += len(chunk)
        if collect_records:
            for row, item in enumerate(chunk):
                count = len(item["markers"])
                records.append(
                    (
                        int(item["qtype"]),
                        logits[row, :count].detach().cpu().numpy(),
                        np.asarray(item["target"], dtype=np.float32),
                        count,
                    )
                )
    metrics = {
        "loss": float(np.mean(losses)),
        "accuracy": float(correct / max(1, total)),
        "examples": total,
    }
    return metrics, records


def calibration_metrics(
    records: list[tuple[int, Any, Any, int]],
    temperature: list[float],
    temperature_by_options: dict[str, float],
) -> dict[str, float]:
    confidences = []
    correctness = []
    losses = []
    soft_losses = []
    briers = []
    for qtype, logits, target, count in records:
        scale = temperature_by_options.get(temp_bucket(qtype, count), temperature[qtype])
        values = np.asarray(logits[:count], dtype=np.float64) / float(scale)
        values -= values.max()
        probabilities = np.exp(values)
        probabilities /= probabilities.sum()
        target_array = np.asarray(target[:count], dtype=np.float64)
        label = int(target_array.argmax())
        predicted = int(probabilities.argmax())
        confidences.append(float(probabilities.max()))
        correctness.append(float(predicted == label))
        losses.append(float(-math.log(max(probabilities[label], 1e-12))))
        soft_losses.append(
            float(-(target_array * np.log(np.clip(probabilities, 1e-12, 1.0))).sum())
        )
        briers.append(float(np.square(probabilities - target_array).sum()))
    bins = 10
    ece = 0.0
    confidence_array = np.asarray(confidences)
    correct_array = np.asarray(correctness)
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        mask = (confidence_array >= lower) & (
            confidence_array <= upper if index == bins - 1 else confidence_array < upper
        )
        if mask.any():
            ece += float(mask.mean()) * abs(float(correct_array[mask].mean()) - float(confidence_array[mask].mean()))
    return {
        "accuracy": float(correct_array.mean()),
        "negative_log_loss": float(np.mean(losses)),
        "soft_target_cross_entropy": float(np.mean(soft_losses)),
        "brier_score": float(np.mean(briers)),
        "expected_calibration_error": ece,
        "mean_confidence": float(confidence_array.mean()),
    }


def checkpoint_config(
    model_config: dict[str, Any], mode: str, dataset_version: str
) -> dict[str, Any]:
    result = dict(model_config)
    result.update(
        {
            "fine_tuned": True,
            "model_name": f"laya-{dataset_version}",
            "dataset_version": dataset_version,
            "experiment_mode": mode,
            "temperature": [1.0, 1.0, 1.0],
        }
    )
    result.pop("temperature_by_options", None)
    return result


def save_checkpoint_atomic(
    model,
    tokenizer,
    model_config: dict[str, Any],
    destination: Path,
    metadata: dict[str, Any],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.tmp-{os.getpid()}"
    recovery = destination.parent / "recovery-model"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir()
    try:
        weights = {
            name: value.detach().to(device="cpu", dtype=torch.float16).contiguous()
            for name, value in model.state_dict().items()
        }
        save_file(weights, str(temporary / "model.safetensors"))
        del weights
        model.encoder.config.save_pretrained(temporary / "encoder")
        tokenizer.save_pretrained(temporary / "tokenizer")
        shutil.copy2(ROUTE_CONTRACT_PATH, temporary / "route_contract.json")
        atomic_write_json(temporary / "rl_agent_config.json", model_config)
        atomic_write_json(temporary / "checkpoint_meta.json", metadata)
        if destination.exists():
            if recovery.exists():
                shutil.rmtree(recovery)
            os.replace(destination, recovery)
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        if not destination.exists() and recovery.exists():
            os.replace(recovery, destination)
        raise


def optimizer_for(model, config: dict[str, Any], strategy: str):
    if strategy == "head_only":
        return torch.optim.AdamW(
            [parameter for parameter in model.parameters() if parameter.requires_grad],
            lr=float(config["learning_rate"]),
            weight_decay=float(config["weight_decay"]),
        )
    encoder = [parameter for parameter in model.encoder.parameters() if parameter.requires_grad]
    head = [
        parameter
        for name, parameter in model.named_parameters()
        if not name.startswith("encoder.") and parameter.requires_grad
    ]
    return torch.optim.AdamW(
        [
            {"params": encoder, "lr": float(config["encoder_learning_rate"])},
            {"params": head, "lr": float(config["learning_rate"])},
        ],
        weight_decay=float(config["weight_decay"]),
    )


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    mode_config = config[args.mode]
    max_steps = int(args.max_steps or mode_config["max_steps"])
    evaluation_interval = int(mode_config["evaluation_interval"])
    patience = int(mode_config.get("patience", config["patience"]))
    grad_accum = int(
        mode_config.get("gradient_accumulation_steps", config["gradient_accumulation_steps"])
    )
    micro_batch = int(mode_config.get("micro_batch_size", config["micro_batch_size"]))
    minimum_epochs = int(mode_config.get("minimum_train_epochs", config.get("minimum_train_epochs", 0)))
    max_length = int(config["max_length"])
    freeze_strategy = str(config["freeze_strategy"])
    require_free_disk(float(config["min_free_disk_gb"]))
    set_seed(int(config["seed"]))
    torch.set_float32_matmul_precision("high")
    device = select_device(args.device or str(config["device"]))
    print(
        f"Mode={args.mode} device={device} precision=float32 strategy={freeze_strategy} "
        f"micro_batch={micro_batch} grad_accum={grad_accum}"
    )
    if device.type == "cpu":
        print("WARNING: training is using CPU; no silent MPS fallback occurred.")

    train_records = read_jsonl(DATA_DIR / "train.jsonl")
    validation_records = read_jsonl(DATA_DIR / "validation.jsonl")
    if args.mode == "smoke":
        train_records = balanced_subset(train_records, int(mode_config["subset_size"]))
    planned_exposures, required_exposures = validate_exposure_plan(
        args.mode,
        max_steps,
        micro_batch,
        grad_accum,
        len(train_records),
        minimum_epochs,
    )

    model, tokenizer, model_config = load_trainable_model(
        BASE_MODEL_DIR, max_length, int(config["head_max_length"])
    )
    trainable = freeze_model(model, freeze_strategy)
    counts = parameter_counts(model)
    print(
        f"Parameters: {counts['total']:,} total; {counts['trainable']:,} trainable "
        f"({counts['trainable'] / counts['total']:.2%})"
    )
    if freeze_strategy != "head_only":
        model.encoder.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False}
        )
        model.head_checkpointing = True
    model.to(device)

    train_items = [build_training_item(tokenizer, model_config, record) for record in train_records]
    validation_items = [
        build_training_item(tokenizer, model_config, record) for record in validation_records
    ]
    print(
        f"Encoded {len(train_items)} train and {len(validation_items)} validation events; "
        f"planned_exposures={planned_exposures} required_exposures={required_exposures}"
    )

    train_features = validation_features = None
    feature_cache_seconds = 0.0
    cache_parity_max_abs = None
    if freeze_strategy == "head_only" and bool(config.get("cache_frozen_encoder", False)):
        cache_batch_size = int(config.get("encoder_cache_batch_size", 16))
        dataset_version = str(config.get("dataset_version", "dataset"))
        train_features, elapsed = build_encoder_cache(
            model,
            train_items,
            tokenizer,
            model_config,
            device,
            f"{dataset_version}-{args.mode}-train",
            cache_batch_size,
        )
        feature_cache_seconds += elapsed
        validation_features, elapsed = build_encoder_cache(
            model,
            validation_items,
            tokenizer,
            model_config,
            device,
            f"{dataset_version}-validation",
            cache_batch_size,
        )
        feature_cache_seconds += elapsed
        parity_batch = move_batch(
            collate_training_items(train_items[:1], tokenizer.pad_token_id, max_length), device
        )
        model.eval()
        with torch.no_grad():
            direct_logits, _ = model(
                parity_batch["input_ids"],
                parity_batch["attention_mask"],
                parity_batch["marker_pos"],
                parity_batch["marker_mask"],
                parity_batch["qtype"],
                detach_encoder=True,
            )
            cached_hidden = torch.from_numpy(np.array(train_features[:1], copy=True)).to(
                device=device, dtype=torch.float32
            )
            cached_logits, _ = forward_from_hidden(model, cached_hidden, parity_batch)
        cache_parity_max_abs = float((direct_logits.float() - cached_logits.float()).abs().max().cpu())
        if cache_parity_max_abs > 0.02:
            raise RuntimeError(
                f"Frozen encoder cache changed native logits by {cache_parity_max_abs:.6f}"
            )
        model.encoder.to("cpu")
        clear_device_cache(device)
        print(f"Frozen-cache parity max_abs={cache_parity_max_abs:.6f}; encoder moved to CPU")

    optimizer = optimizer_for(model, config, freeze_strategy)
    warmup_steps = int(config["warmup_steps"])

    def learning_rate_multiplier(step: int) -> float:
        if warmup_steps and step < warmup_steps:
            return max(1e-3, (step + 1) / warmup_steps)
        progress = (step - warmup_steps) / max(1, max_steps - warmup_steps)
        return max(0.05, 0.5 * (1.0 + math.cos(math.pi * min(1.0, progress))))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, learning_rate_multiplier)
    optimizer.zero_grad(set_to_none=True)
    initial_validation, _ = evaluate_model(
        model,
        validation_items,
        tokenizer,
        max_length,
        int(config["validation_batch_size"]),
        device,
        freeze_strategy,
        feature_cache=validation_features,
    )
    print(f"Initial validation: {initial_validation}")

    output_path = OUTPUT_DIR / ("smoke-model" if args.mode == "smoke" else "best-model")
    model_config_to_save = checkpoint_config(
        model_config, args.mode, str(config.get("dataset_version", "unknown"))
    )
    history: list[dict[str, Any]] = []
    best_accuracy = -1.0
    best_loss = float("inf")
    if args.mode == "experiment" and (output_path / "checkpoint_meta.json").exists():
        with (output_path / "checkpoint_meta.json").open(encoding="utf-8") as handle:
            previous_best = json.load(handle).get("validation", {})
        best_accuracy = float(previous_best.get("accuracy", best_accuracy))
        best_loss = float(previous_best.get("loss", best_loss))
        print(
            f"Existing best to beat: accuracy={best_accuracy:.3f}, loss={best_loss:.4f}"
        )
    stale_evaluations = 0
    checkpoint_updated = False
    global_step = 0
    micro_step = 0
    examples_exposed = 0
    epoch = 0
    cursor = 0
    order = list(range(len(train_items)))
    minimum_exposures = minimum_epochs * len(train_items)
    run_started = time.perf_counter()
    set_training_mode(model, freeze_strategy)

    while global_step < max_steps:
        if cursor == 0:
            random.Random(int(config["seed"]) + epoch).shuffle(order)
        end = min(cursor + micro_batch, len(order))
        batch_indices = order[cursor:end]
        chunk = [train_items[index] for index in batch_indices]
        cursor = end
        if cursor >= len(order):
            cursor = 0
            epoch += 1

        batch = move_batch(
            collate_training_items(chunk, tokenizer.pad_token_id, max_length), device
        )
        examples_exposed += len(chunk)
        progress = global_step / max(1, max_steps - 1)
        sigma = float(config["sigma_start"]) + (
            float(config["sigma_end"]) - float(config["sigma_start"])
        ) * progress
        hidden_states = None
        if train_features is not None:
            hidden_states = torch.from_numpy(
                np.array(train_features[batch_indices], copy=True)
            ).to(device=device, dtype=torch.float32)
        loss, components = forward_loss(
            model,
            batch,
            freeze_strategy,
            sigma,
            int(config["rlcd_group_size"]),
            hidden_states=hidden_states,
        )
        if not torch.isfinite(loss):
            raise FloatingPointError(
                f"Non-finite training loss at optimizer step {global_step}, micro step {micro_step}: {components}"
            )
        (loss / grad_accum).backward()
        micro_step += 1
        if micro_step % grad_accum:
            continue

        gradient_norm = torch.nn.utils.clip_grad_norm_(trainable, 1.0)
        if not torch.isfinite(gradient_norm):
            raise FloatingPointError(f"Non-finite gradient norm at optimizer step {global_step}")
        optimizer.step()
        scheduler.step()
        optimizer.zero_grad(set_to_none=True)
        global_step += 1

        record = {
            "step": global_step,
            "epoch": epoch,
            "examples_exposed": examples_exposed,
            "sigma": sigma,
            "learning_rates": [group["lr"] for group in optimizer.param_groups],
            "gradient_norm": float(gradient_norm.detach().cpu()),
            **components,
        }
        history.append(record)
        print(
            f"step {global_step:03d}/{max_steps} loss={components['loss']:.4f} "
            f"ce={components['loss_ce']:.4f} reward={components['reward']:.4f}"
        )

        should_evaluate = global_step % evaluation_interval == 0 or global_step == max_steps
        if not should_evaluate:
            continue
        validation, _ = evaluate_model(
            model,
            validation_items,
            tokenizer,
            max_length,
            int(config["validation_batch_size"]),
            device,
            freeze_strategy,
            feature_cache=validation_features,
        )
        record["validation"] = validation
        improved = validation["accuracy"] > best_accuracy or (
            validation["accuracy"] == best_accuracy and validation["loss"] < best_loss
        )
        print(f"validation step {global_step}: {validation} improved={improved}")
        if improved:
            best_accuracy = validation["accuracy"]
            best_loss = validation["loss"]
            stale_evaluations = 0
            save_checkpoint_atomic(
                model,
                tokenizer,
                model_config_to_save,
                output_path,
                {
                    "mode": args.mode,
                    "step": global_step,
                    "examples_exposed": examples_exposed,
                    "dataset_version": config.get("dataset_version"),
                    "validation": validation,
                    "freeze_strategy": freeze_strategy,
                },
            )
            checkpoint_updated = True
            clear_device_cache(device)
        else:
            stale_evaluations += 1
        if (
            args.mode == "experiment"
            and examples_exposed >= minimum_exposures
            and stale_evaluations >= patience
        ):
            print(
                f"Early stopping after {examples_exposed} examples and "
                f"{stale_evaluations} evaluations without improvement"
            )
            break
        set_training_mode(model, freeze_strategy)

    training_seconds = time.perf_counter() - run_started
    if not output_path.exists():
        validation, _ = evaluate_model(
            model,
            validation_items,
            tokenizer,
            max_length,
            int(config["validation_batch_size"]),
            device,
            freeze_strategy,
            feature_cache=validation_features,
        )
        save_checkpoint_atomic(
            model,
            tokenizer,
            model_config_to_save,
            output_path,
            {
                "mode": args.mode,
                "step": global_step,
                "examples_exposed": examples_exposed,
                "dataset_version": config.get("dataset_version"),
                "validation": validation,
                "freeze_strategy": freeze_strategy,
            },
        )
        checkpoint_updated = True

    if checkpoint_updated:
        with (output_path / "checkpoint_meta.json").open(encoding="utf-8") as handle:
            selected_meta = json.load(handle)
        selected_meta["training_duration_seconds"] = training_seconds
        selected_meta["max_steps_requested"] = max_steps
        atomic_write_json(output_path / "checkpoint_meta.json", selected_meta)

    calibration = None
    if args.mode == "experiment":
        # Reload the atomically saved best weights, then fit only on validation records.
        model.load_state_dict(load_file(str(output_path / "model.safetensors")), strict=True)
        model.float()
        if validation_features is None:
            model.to(device)
        validation, calibration_records = evaluate_model(
            model,
            validation_items,
            tokenizer,
            max_length,
            int(config["validation_batch_size"]),
            device,
            freeze_strategy,
            collect_records=True,
            feature_cache=validation_features,
        )
        fitted = fit_temperature_map(calibration_records)
        temperatures = [float(value) for value in fitted["temperature"]]
        option_temperatures = {
            key: float(value) for key, value in fitted["temperature_by_options"].items()
        }
        thresholds = fit_abstention_thresholds(
            calibration_records,
            temperatures,
            option_temperatures,
            target_error=float(config["calibration_target_error"]),
            min_bucket_n=10,
            conservative=True,
        )
        before = calibration_metrics(calibration_records, [1.0, 1.0, 1.0], {})
        after = calibration_metrics(calibration_records, temperatures, option_temperatures)
        with (output_path / "checkpoint_meta.json").open(encoding="utf-8") as handle:
            checkpoint_meta = json.load(handle)
        identity_payload = {
            "model_name": model_config_to_save["model_name"],
            "mode": checkpoint_meta.get("mode"),
            "step": checkpoint_meta.get("step"),
            "question_contract_sha256": question_contract_hash(),
            "temperature": temperatures,
            "abstention_thresholds": thresholds,
        }
        calibration_id = hashlib.sha256(
            json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        calibration = {
            "calibration_id": calibration_id,
            "checkpoint_identity": identity_payload,
            "fit_split": "validation",
            "n": len(calibration_records),
            "temperature": temperatures,
            "temperature_by_options": option_temperatures,
            "abstention_thresholds": thresholds,
            "target_error": float(config["calibration_target_error"]),
            "metrics_before": before,
            "metrics_after": after,
        }
        with (output_path / "rl_agent_config.json").open(encoding="utf-8") as handle:
            saved_config = json.load(handle)
        saved_config["temperature"] = temperatures
        saved_config["temperature_by_options"] = option_temperatures
        saved_config["calibration_id"] = calibration_id
        checkpoint_meta["calibration_id"] = calibration_id
        atomic_write_json(output_path / "rl_agent_config.json", saved_config)
        atomic_write_json(output_path / "checkpoint_meta.json", checkpoint_meta)
        atomic_write_json(output_path / "calibration.json", calibration)
        # Convenience copy for run inspection; inference never trusts this unbound file.
        atomic_write_json(OUTPUT_DIR / "calibration.json", calibration)
        print(f"Validation calibration: {json.dumps(calibration, indent=2)}")

    history_payload = {
        "mode": args.mode,
        "device": str(device),
        "precision": "float32",
        "freeze_strategy": freeze_strategy,
        "parameter_counts": counts,
        "train_examples": len(train_items),
        "validation_examples": len(validation_items),
        "initial_validation": initial_validation,
        "best_validation": {"accuracy": best_accuracy, "loss": best_loss},
        "optimizer_steps": global_step,
        "micro_batch_size": micro_batch,
        "gradient_accumulation_steps": grad_accum,
        "examples_exposed": examples_exposed,
        "epochs_completed": examples_exposed / max(1, len(train_items)),
        "minimum_train_epochs": minimum_epochs,
        "feature_cache_seconds": feature_cache_seconds,
        "feature_cache_dtype": "float16" if train_features is not None else None,
        "feature_cache_contains_labels": False if train_features is not None else None,
        "feature_cache_parity_max_abs": cache_parity_max_abs,
        "duration_seconds": training_seconds,
        "mps_memory_at_end": mps_memory(),
        "history": history,
        "calibration": calibration,
        "checkpoint": str(output_path),
        "selected_checkpoint_updated": checkpoint_updated,
    }
    atomic_write_json(OUTPUT_DIR / f"training_history_{args.mode}.json", history_payload)
    attempts_path = OUTPUT_DIR / "training_attempts.json"
    attempts = []
    if attempts_path.exists():
        with attempts_path.open(encoding="utf-8") as handle:
            attempts = json.load(handle)
    attempts.append(
        {
            "mode": args.mode,
            "device": str(device),
            "max_steps_requested": max_steps,
            "optimizer_steps_completed": global_step,
            "micro_batch_size": micro_batch,
            "gradient_accumulation_steps": grad_accum,
            "examples_exposed": examples_exposed,
            "epochs_completed": examples_exposed / max(1, len(train_items)),
            "duration_seconds": training_seconds,
            "selected_checkpoint_updated": checkpoint_updated,
            "best_validation": {"accuracy": best_accuracy, "loss": best_loss},
        }
    )
    atomic_write_json(attempts_path, attempts)
    update_manifest(**{f"training_{args.mode}": history_payload, "training_attempts": attempts})
    print(
        f"Training complete in {training_seconds:.1f}s; best validation accuracy={best_accuracy:.3f}; "
        f"checkpoint={output_path}"
    )


if __name__ == "__main__":
    with exclusive_training():
        main()
