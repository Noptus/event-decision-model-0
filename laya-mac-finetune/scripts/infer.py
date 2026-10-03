#!/usr/bin/env python3
"""Run native Laya choice inference with an optional calibrated review gate."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import torch
from laya import Agent

from _shared import (
    BASE_MODEL_DIR,
    CONFIG_PATH,
    OUTPUT_DIR,
    QUESTION_ID,
    load_config,
    load_routing_question,
    question_contract_hash,
    read_jsonl,
    select_device,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--event", help="One JSON business event")
    source.add_argument("--file", type=Path, help="JSONL business events")
    source.add_argument("--interactive", action="store_true", help="Read one JSON event per prompt")
    parser.add_argument(
        "--model",
        default=str(OUTPUT_DIR / "best-model"),
        help="Checkpoint directory, or 'base' for the untouched multilingual model",
    )
    parser.add_argument("--device", choices=("auto", "mps", "cpu"))
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--no-review-gate", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def sync(device: torch.device) -> None:
    if device.type == "mps" and hasattr(torch, "mps"):
        torch.mps.synchronize()


def parse_event(text: str) -> dict[str, Any]:
    try:
        event = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid event JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise ValueError("An event must be a JSON object")
    return event.get("state", event)


def load_events(args: argparse.Namespace) -> tuple[list[dict[str, Any]], bool]:
    if args.event:
        return [parse_event(args.event)], False
    if args.file:
        return [record.get("state", record) for record in read_jsonl(args.file)], False
    if not args.interactive and not sys.stdin.isatty():
        return [parse_event(line) for line in sys.stdin if line.strip()], False
    return [], True


def review_thresholds(
    model_path: Path, agent: Agent, disabled: bool, routing_question: dict[str, Any]
) -> dict[str, float] | None:
    if disabled or not agent.cfg.get("fine_tuned"):
        return None
    # A policy is valid only beside the checkpoint that produced it. Never borrow the
    # best-model policy for a smoke run or an arbitrary custom model.
    calibration_path = model_path / "calibration.json"
    if not calibration_path.exists():
        return None
    with calibration_path.open(encoding="utf-8") as handle:
        calibration = json.load(handle)
    calibration_id = calibration.get("calibration_id")
    if not calibration_id or calibration_id != agent.cfg.get("calibration_id"):
        raise RuntimeError(
            f"Calibration identity does not match checkpoint {model_path}; refusing to apply its review policy."
        )
    identity = calibration.get("checkpoint_identity", {})
    if identity.get("question_contract_sha256") != question_contract_hash(routing_question):
        raise RuntimeError("Calibration was fitted for a different routing question contract")
    values = calibration.get("abstention_thresholds", {})
    return {str(key): float(value) for key, value in values.items()} or None


def format_result(
    result: dict[str, Any],
    model_path: Path,
    agent: Agent,
    latency_ms: float,
    verbose: bool,
) -> dict[str, Any]:
    answer = result["answers"][QUESTION_ID]
    abstention = answer.get("abstention", "not_configured")
    output = {
        "selected_route": answer["choice"],
        "probabilities": answer["probabilities"],
        "confidence": answer["answer_confidence"],
        "review_required": abstention == "abstained",
        "review_status": abstention,
        "review_threshold": answer.get("abstention_threshold"),
        "model": {
            "path": str(model_path),
            "name": agent.cfg.get("model_name", "unknown"),
            "fine_tuned": bool(agent.cfg.get("fine_tuned", False)),
        },
        "device": str(agent.device),
        "latency_ms": round(latency_ms, 3),
    }
    if verbose:
        output["raw_laya_answer"] = result
    return output


def run_batch(
    agent: Agent,
    model_path: Path,
    events: list[dict[str, Any]],
    routing_question: dict[str, Any],
    config: dict[str, Any],
    thresholds: dict[str, float] | None,
    batch_size: int,
    verbose: bool,
) -> list[dict[str, Any]]:
    if not events:
        return []
    sync(agent.device)
    started = time.perf_counter()
    results = agent.predict_batch(
        events,
        routing_question,
        batch_size=batch_size,
        sort_by_length=len(events) > batch_size,
        max_len=int(config["max_length"]),
        head_max_len=int(config["head_max_length"]),
        min_confidence=thresholds,
    )
    sync(agent.device)
    elapsed_ms = (time.perf_counter() - started) * 1000
    amortized_ms = elapsed_ms / len(events)
    return [
        format_result(result, model_path, agent, amortized_ms, verbose) for result in results
    ]


def main() -> None:
    args = parse_args()
    config = load_config(CONFIG_PATH)
    requested_device = select_device(args.device or str(config["device"]))
    model_path = BASE_MODEL_DIR if args.model == "base" else Path(args.model).expanduser()
    if not model_path.is_dir():
        raise SystemExit(f"Checkpoint directory does not exist: {model_path}")
    started = time.perf_counter()
    agent = Agent(str(model_path), device=str(requested_device), compile=False, fast=False)
    sync(agent.device)
    load_ms = (time.perf_counter() - started) * 1000
    if agent.device.type != requested_device.type:
        raise RuntimeError(
            f"Laya changed device from {requested_device} to {agent.device}; rerun with --device cpu."
        )
    routing_question = load_routing_question(model_path)
    thresholds = review_thresholds(model_path, agent, args.no_review_gate, routing_question)
    batch_size = args.batch_size or int(config["inference_batch_size"])
    events, interactive = load_events(args)
    print(
        json.dumps(
            {
                "loaded_model": str(model_path),
                "device": str(agent.device),
                "load_ms": round(load_ms, 1),
                "review_thresholds": thresholds,
            }
        )
    )

    if not interactive:
        for output in run_batch(
            agent, model_path, events, routing_question, config, thresholds, batch_size, args.verbose
        ):
            print(json.dumps(output, ensure_ascii=False))
        return

    latencies = []
    while True:
        try:
            text = input("event JSON (blank or 'quit' to stop)> ").strip()
        except EOFError:
            break
        if not text or text.lower() in {"quit", "exit"}:
            break
        try:
            outputs = run_batch(
                agent,
                model_path,
                [parse_event(text)],
                routing_question,
                config,
                thresholds,
                1,
                args.verbose,
            )
            latencies.append(outputs[0]["latency_ms"])
            print(json.dumps(outputs[0], ensure_ascii=False, indent=2))
        except (ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
    if latencies:
        print(json.dumps({"warm_median_latency_ms": statistics.median(latencies)}))


if __name__ == "__main__":
    main()
