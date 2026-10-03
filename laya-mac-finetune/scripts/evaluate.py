#!/usr/bin/env python3
"""Compare the untouched base and fine-tuned Laya checkpoints on held-out events."""
from __future__ import annotations

import argparse
import csv
import gc
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import torch
from laya import Agent

from _shared import (
    BASE_MODEL_DIR,
    CONFIG_PATH,
    DATA_DIR,
    OUTPUT_DIR,
    QUESTION_ID,
    ROUTES,
    ROUTING_QUESTION,
    atomic_write_json,
    clear_device_cache,
    load_config,
    probabilities_from_answer,
    read_jsonl,
    select_device,
    update_manifest,
    write_jsonl,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"))
    parser.add_argument("--fine-tuned", type=Path, default=OUTPUT_DIR / "best-model")
    return parser.parse_args()


def sync(device: torch.device) -> None:
    if device.type == "mps" and hasattr(torch, "mps"):
        torch.mps.synchronize()


def load_checked_agent(path: Path, device: torch.device) -> tuple[Agent, float]:
    started = time.perf_counter()
    agent = Agent(str(path), device=str(device), compile=False, fast=False)
    sync(agent.device)
    load_seconds = time.perf_counter() - started
    if agent.device.type != device.type:
        raise RuntimeError(
            f"Laya changed device from requested {device} to {agent.device}; rerun explicitly with --device cpu."
        )
    return agent, load_seconds


def predict_all(
    agent: Agent,
    states: list[dict[str, Any]],
    max_length: int,
    head_max_length: int,
    batch_size: int,
    min_confidence: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    sync(agent.device)
    started = time.perf_counter()
    results = agent.predict_batch(
        states,
        ROUTING_QUESTION,
        batch_size=batch_size,
        sort_by_length=True,
        max_len=max_length,
        head_max_len=head_max_length,
        min_confidence=min_confidence,
    )
    sync(agent.device)
    return results, time.perf_counter() - started


def warm_latency(
    agent: Agent, state: dict[str, Any], max_length: int, head_max_length: int, runs: int = 5
) -> dict[str, float]:
    agent.predict(state, ROUTING_QUESTION, max_len=max_length, head_max_len=head_max_length)
    sync(agent.device)
    values = []
    for _ in range(runs):
        sync(agent.device)
        started = time.perf_counter()
        agent.predict(state, ROUTING_QUESTION, max_len=max_length, head_max_len=head_max_length)
        sync(agent.device)
        values.append((time.perf_counter() - started) * 1000)
    return {
        "runs": runs,
        "median_ms": float(statistics.median(values)),
        "p95_ms": float(np.percentile(values, 95)),
        "min_ms": float(min(values)),
    }


def metrics_for(records: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    correct = []
    confidences = []
    hard_nll = []
    soft_cross_entropy = []
    hard_brier = []
    soft_brier = []
    per_route: dict[str, list[float]] = defaultdict(list)
    per_language: dict[str, list[float]] = defaultdict(list)
    per_challenge: dict[str, list[float]] = defaultdict(list)
    abstained = []
    accepted_correct = []
    for record, result in zip(records, results):
        answer = result["answers"][QUESTION_ID]
        probabilities = np.asarray(probabilities_from_answer(answer), dtype=np.float64)
        truth = record["gold"][QUESTION_ID]["label"]
        truth_index = ROUTES.index(truth)
        target = np.asarray(
            [record["gold"][QUESTION_ID]["probabilities"][route] for route in ROUTES],
            dtype=np.float64,
        )
        prediction = answer["choice"]
        is_correct = float(prediction == truth)
        confidence = float(answer["answer_confidence"])
        one_hot = np.zeros(len(ROUTES), dtype=np.float64)
        one_hot[truth_index] = 1.0
        correct.append(is_correct)
        confidences.append(confidence)
        hard_nll.append(-math.log(max(float(probabilities[truth_index]), 1e-12)))
        soft_cross_entropy.append(float(-(target * np.log(np.clip(probabilities, 1e-12, 1.0))).sum()))
        hard_brier.append(float(np.square(probabilities - one_hot).sum()))
        soft_brier.append(float(np.square(probabilities - target).sum()))
        per_route[truth].append(is_correct)
        per_language[record["language"]].append(is_correct)
        for tag in record.get("challenge_tags", []):
            per_challenge[tag].append(is_correct)
        if "abstention" in answer:
            did_abstain = answer["abstention"] == "abstained"
            abstained.append(float(did_abstain))
            if not did_abstain:
                accepted_correct.append(is_correct)

    confidence_array = np.asarray(confidences)
    correct_array = np.asarray(correct)
    ece = 0.0
    for index in range(10):
        lower, upper = index / 10, (index + 1) / 10
        mask = (confidence_array >= lower) & (
            confidence_array <= upper if index == 9 else confidence_array < upper
        )
        if mask.any():
            ece += float(mask.mean()) * abs(float(correct_array[mask].mean()) - float(confidence_array[mask].mean()))

    result = {
        "count": len(records),
        "accuracy": float(correct_array.mean()),
        "accuracy_per_route": {
            route: {"accuracy": float(np.mean(per_route[route])), "count": len(per_route[route])}
            for route in ROUTES
        },
        "accuracy_per_language": {
            key: {"accuracy": float(np.mean(values)), "count": len(values)}
            for key, values in sorted(per_language.items())
        },
        "accuracy_per_challenge": {
            key: {"accuracy": float(np.mean(values)), "count": len(values)}
            for key, values in sorted(per_challenge.items())
        },
        "negative_log_loss": float(np.mean(hard_nll)),
        "soft_target_cross_entropy": float(np.mean(soft_cross_entropy)),
        "brier_score": float(np.mean(hard_brier)),
        "soft_target_brier_score": float(np.mean(soft_brier)),
        "expected_calibration_error_10_bins": ece,
        "mean_confidence": float(confidence_array.mean()),
    }
    if abstained:
        result["review_policy"] = {
            "review_rate": float(np.mean(abstained)),
            "coverage": float(1.0 - np.mean(abstained)),
            "accepted_accuracy": float(np.mean(accepted_correct)) if accepted_correct else None,
        }
    return result


def confusion(records: list[dict[str, Any]], results: list[dict[str, Any]]) -> Counter:
    values = Counter()
    for record, result in zip(records, results):
        truth = record["gold"][QUESTION_ID]["label"]
        predicted = result["answers"][QUESTION_ID]["choice"]
        values[(truth, predicted)] += 1
    return values


def release(agent: Agent) -> None:
    device = agent.device
    model = agent.model
    agent.model = None
    del model
    gc.collect()
    clear_device_cache(device)


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    device = select_device(args.device or str(config["device"]))
    records = read_jsonl(DATA_DIR / "test.jsonl")
    if any(record["questions"] != ROUTING_QUESTION for record in records):
        raise RuntimeError("Test records do not share the expected Laya question contract")
    states = [record["state"] for record in records]
    max_length = int(config["max_length"])
    head_max_length = int(config["head_max_length"])
    batch_size = int(config["inference_batch_size"])

    print(f"Loading untouched base checkpoint on {device} ...")
    base_agent, base_load_seconds = load_checked_agent(BASE_MODEL_DIR, device)
    base_results, base_eval_seconds = predict_all(
        base_agent, states, max_length, head_max_length, batch_size
    )
    base_latency = warm_latency(base_agent, states[0], max_length, head_max_length)
    base_metrics = metrics_for(records, base_results)
    release(base_agent)

    calibration_path = args.fine_tuned / "calibration.json"
    with calibration_path.open(encoding="utf-8") as handle:
        calibration = json.load(handle)
    thresholds = {
        key: float(value) for key, value in calibration.get("abstention_thresholds", {}).items()
    }

    print(f"Loading fine-tuned checkpoint on {device} ...")
    fine_agent, fine_load_seconds = load_checked_agent(args.fine_tuned, device)
    if calibration.get("calibration_id") != fine_agent.cfg.get("calibration_id"):
        raise RuntimeError("Fine-tuned checkpoint and calibration metadata do not match")
    calibrated_temperature = list(fine_agent.temperature)
    calibrated_by_options = dict(fine_agent.temperature_by_options)
    fine_agent.temperature = [1.0, 1.0, 1.0]
    fine_agent.temperature_by_options = {}
    fine_uncalibrated_results, fine_uncalibrated_seconds = predict_all(
        fine_agent, states, max_length, head_max_length, batch_size
    )
    fine_agent.temperature = calibrated_temperature
    fine_agent.temperature_by_options = calibrated_by_options
    fine_results, fine_eval_seconds = predict_all(
        fine_agent,
        states,
        max_length,
        head_max_length,
        batch_size,
        thresholds or None,
    )
    fine_latency = warm_latency(fine_agent, states[0], max_length, head_max_length)
    fine_uncalibrated_metrics = metrics_for(records, fine_uncalibrated_results)
    fine_metrics = metrics_for(records, fine_results)
    release(fine_agent)

    improvements = []
    regressions = []
    probability_changes = []
    prediction_rows = []
    for record, base_result, raw_result, fine_result in zip(
        records, base_results, fine_uncalibrated_results, fine_results
    ):
        truth = record["gold"][QUESTION_ID]["label"]
        base_answer = base_result["answers"][QUESTION_ID]
        fine_answer = fine_result["answers"][QUESTION_ID]
        row = {
            "id": record["id"],
            "language": record["language"],
            "challenge_tags": record["challenge_tags"],
            "state": record["state"],
            "truth": truth,
            "base": base_answer,
            "fine_tuned_uncalibrated": raw_result["answers"][QUESTION_ID],
            "fine_tuned": fine_answer,
            "review_required": fine_answer.get("abstention") == "abstained",
        }
        prediction_rows.append(row)
        base_correct = base_answer["choice"] == truth
        fine_correct = fine_answer["choice"] == truth
        base_truth_probability = float(base_answer["probabilities"][truth])
        fine_truth_probability = float(fine_answer["probabilities"][truth])
        summary = {
            "id": record["id"],
            "language": record["language"],
            "challenge_tags": record["challenge_tags"],
            "state": record["state"],
            "truth": truth,
            "base_choice": base_answer["choice"],
            "base_confidence": base_answer["answer_confidence"],
            "base_truth_probability": base_truth_probability,
            "fine_tuned_choice": fine_answer["choice"],
            "fine_tuned_confidence": fine_answer["answer_confidence"],
            "fine_tuned_truth_probability": fine_truth_probability,
            "truth_probability_delta": fine_truth_probability - base_truth_probability,
        }
        probability_changes.append(summary)
        if not base_correct and fine_correct:
            improvements.append(summary)
        elif base_correct and not fine_correct:
            regressions.append(summary)

    probability_improvements = sorted(
        (item for item in probability_changes if item["truth_probability_delta"] > 0),
        key=lambda item: item["truth_probability_delta"],
        reverse=True,
    )[:5]
    probability_regressions = sorted(
        (item for item in probability_changes if item["truth_probability_delta"] < 0),
        key=lambda item: item["truth_probability_delta"],
    )[:5]

    write_jsonl(OUTPUT_DIR / "predictions.jsonl", prediction_rows)
    confusion_rows = []
    for model_name, results in (("base", base_results), ("fine_tuned", fine_results)):
        counts = confusion(records, results)
        for truth in ROUTES:
            for predicted in ROUTES:
                confusion_rows.append(
                    {
                        "model": model_name,
                        "true_route": truth,
                        "predicted_route": predicted,
                        "count": counts[(truth, predicted)],
                    }
                )
    with (OUTPUT_DIR / "confusion_matrix.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=("model", "true_route", "predicted_route", "count")
        )
        writer.writeheader()
        writer.writerows(confusion_rows)

    training_seconds = None
    training_search_seconds = None
    checkpoint_meta_path = args.fine_tuned / "checkpoint_meta.json"
    if checkpoint_meta_path.exists():
        with checkpoint_meta_path.open(encoding="utf-8") as handle:
            checkpoint_meta = json.load(handle)
        training_seconds = checkpoint_meta.get("training_duration_seconds")
        training_search_seconds = checkpoint_meta.get("total_search_duration_seconds", training_seconds)
    if training_seconds is None:
        history_path = OUTPUT_DIR / "training_history_experiment.json"
        if history_path.exists():
            with history_path.open(encoding="utf-8") as handle:
                training_seconds = json.load(handle).get("duration_seconds")
            training_search_seconds = training_seconds
    report = {
        "status": "experimental_synthetic_proof_of_concept",
        "device": str(device),
        "test_events": len(records),
        "question_contract": ROUTING_QUESTION,
        "base": {
            "checkpoint": str(BASE_MODEL_DIR),
            "metrics": base_metrics,
            "load_seconds": base_load_seconds,
            "batched_evaluation_seconds": base_eval_seconds,
            "warm_single_event_latency": base_latency,
        },
        "fine_tuned_uncalibrated": {
            "checkpoint": str(args.fine_tuned),
            "metrics": fine_uncalibrated_metrics,
            "batched_evaluation_seconds": fine_uncalibrated_seconds,
        },
        "fine_tuned": {
            "checkpoint": str(args.fine_tuned),
            "metrics": fine_metrics,
            "load_seconds": fine_load_seconds,
            "batched_evaluation_seconds": fine_eval_seconds,
            "warm_single_event_latency": fine_latency,
        },
        "calibration": calibration,
        "selected_checkpoint_training_duration_seconds": training_seconds,
        "total_training_search_duration_seconds": training_search_seconds,
        "argmax_improvements": improvements,
        "argmax_regressions": regressions,
        "probability_improvements": probability_improvements,
        "probability_regressions": probability_regressions,
        "notes": [
            "The data are deterministic synthetic events, not production traffic.",
            "Calibration and the review threshold were fit only on validation, never on test.",
            "The review gate preserves the native route choice and probabilities.",
        ],
    }
    atomic_write_json(OUTPUT_DIR / "metrics.json", report)
    update_manifest(evaluation={
        "device": str(device),
        "base_accuracy": base_metrics["accuracy"],
        "fine_tuned_accuracy": fine_metrics["accuracy"],
        "fine_tuned_uncalibrated_accuracy": fine_uncalibrated_metrics["accuracy"],
        "test_events": len(records),
        "metrics_path": str(OUTPUT_DIR / "metrics.json"),
    })
    print(
        json.dumps(
            {
                "device": str(device),
                "base_accuracy": base_metrics["accuracy"],
                "fine_tuned_accuracy": fine_metrics["accuracy"],
                "base_nll": base_metrics["negative_log_loss"],
                "fine_tuned_nll": fine_metrics["negative_log_loss"],
                "fine_tuned_ece": fine_metrics["expected_calibration_error_10_bins"],
                "improvements": len(improvements),
                "regressions": len(regressions),
                "review_policy": fine_metrics.get("review_policy"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
