#!/usr/bin/env python3
"""Conservative single-process Laya fine-tuning for Apple Silicon."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import time
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


def forward_loss(
    model,
    batch: dict[str, torch.Tensor],
    freeze_strategy: str,
    sigma: float,
    group_size: int,
) -> tuple[torch.Tensor, dict[str, float]]:
    logits, activation = model(
        batch["input_ids"],
        batch["attention_mask"],
        batch["marker_pos"],
        batch["marker_mask"],
        batch["qtype"],
        detach_encoder=freeze_strategy == "head_only",
    )
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
) -> tuple[dict[str, float], list[tuple[int, Any, Any, int]]]:
    model.eval()
    losses: list[float] = []
    correct = 0
    total = 0
    records = []
    for start in range(0, len(items), batch_size):
        chunk = items[start : start + batch_size]
        batch = move_batch(collate_training_items(chunk, tokenizer.pad_token_id, max_length), device)
        logits, _ = model(
            batch["input_ids"],
            batch["attention_mask"],
            batch["marker_pos"],
            batch["marker_mask"],
            batch["qtype"],
            detach_encoder=freeze_strategy == "head_only",
        )
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


def checkpoint_config(model_config: dict[str, Any], mode: str) -> dict[str, Any]:
    result = dict(model_config)
    result.update(
        {
            "fine_tuned": True,
            "model_name": "laya-solace-event-mesh-router",
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
    max_length = int(config["max_length"])
    freeze_strategy = str(config["freeze_strategy"])
    require_free_disk(float(config["min_free_disk_gb"]))
    set_seed(int(config["seed"]))
    torch.set_float32_matmul_precision("high")
    device = select_device(args.device or str(config["device"]))
    print(f"Mode={args.mode} device={device} precision=float32 strategy={freeze_strategy}")
    if device.type == "cpu":
        print("WARNING: training is using CPU; no silent MPS fallback occurred.")

    train_records = read_jsonl(DATA_DIR / "train.jsonl")
    validation_records = read_jsonl(DATA_DIR / "validation.jsonl")
    if args.mode == "smoke":
        train_records = balanced_subset(train_records, int(mode_config["subset_size"]))

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
    print(f"Encoded {len(train_items)} train and {len(validation_items)} validation events")

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
    )
    print(f"Initial validation: {initial_validation}")

    output_path = OUTPUT_DIR / ("smoke-model" if args.mode == "smoke" else "best-model")
    model_config_to_save = checkpoint_config(model_config, args.mode)
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
    epoch = 0
    cursor = 0
    order = list(range(len(train_items)))
    run_started = time.perf_counter()
    set_training_mode(model, freeze_strategy)

    while global_step < max_steps:
        if cursor == 0:
            random.Random(int(config["seed"]) + epoch).shuffle(order)
        item = train_items[order[cursor]]
        cursor += 1
        if cursor >= len(order):
            cursor = 0
            epoch += 1

        batch = move_batch(
            collate_training_items([item], tokenizer.pad_token_id, max_length), device
        )
        progress = global_step / max(1, max_steps - 1)
        sigma = float(config["sigma_start"]) + (
            float(config["sigma_end"]) - float(config["sigma_start"])
        ) * progress
        loss, components = forward_loss(
            model,
            batch,
            freeze_strategy,
            sigma,
            int(config["rlcd_group_size"]),
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
                    "validation": validation,
                    "freeze_strategy": freeze_strategy,
                },
            )
            checkpoint_updated = True
            clear_device_cache(device)
        else:
            stale_evaluations += 1
        if args.mode == "experiment" and stale_evaluations >= patience:
            print(f"Early stopping after {stale_evaluations} evaluations without improvement")
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
        )
        save_checkpoint_atomic(
            model,
            tokenizer,
            model_config_to_save,
            output_path,
            {"mode": args.mode, "step": global_step, "validation": validation},
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
        model.float().to(device)
        validation, calibration_records = evaluate_model(
            model,
            validation_items,
            tokenizer,
            max_length,
            int(config["validation_batch_size"]),
            device,
            freeze_strategy,
            collect_records=True,
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
            "model_name": "laya-solace-event-mesh-router",
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
        "gradient_accumulation_steps": grad_accum,
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
    main()
