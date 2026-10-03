#!/usr/bin/env python3
"""Persistent JSONL model worker. Stdout is reserved for protocol messages."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from _shared import QUESTION_ID, ROUTING_QUESTION, load_config, question_contract_hash, select_device  # noqa: E402

PROTOCOL_VERSION = 1


def log(message: str) -> None:
    print(f"[laya-worker] {message}", file=sys.stderr, flush=True)


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--max-event-bytes", type=int, default=256 * 1024)
    return parser.parse_args()


def load_review_thresholds(model_path: Path, agent) -> dict[str, float] | None:
    if not agent.cfg.get("fine_tuned"):
        return None
    path = model_path / "calibration.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        calibration = json.load(handle)
    calibration_id = calibration.get("calibration_id")
    if not calibration_id or calibration_id != agent.cfg.get("calibration_id"):
        raise RuntimeError("checkpoint calibration identity mismatch")
    identity = calibration.get("checkpoint_identity", {})
    if identity.get("question_contract_sha256") != question_contract_hash():
        raise RuntimeError("checkpoint calibration uses a different question contract")
    thresholds = calibration.get("abstention_thresholds", {})
    return {str(key): float(value) for key, value in thresholds.items()} or None


def drain_oversized_line(stream) -> None:
    while True:
        fragment = stream.readline(64 * 1024)
        if not fragment or fragment.endswith(b"\n"):
            return


def request_id(value: Any) -> str:
    return value if isinstance(value, str) and value else "unknown"


def main() -> None:
    args = parse_args()
    if args.max_event_bytes < 1024:
        raise SystemExit("--max-event-bytes must be at least 1024")
    model_path = Path(args.model).expanduser().resolve()
    config = load_config()

    # Delay the heavyweight import until arguments and local paths are known. Laya and transformer
    # diagnostics use stderr; stdout remains a strict JSONL protocol channel.
    from laya import Agent
    import torch

    requested_device = select_device(args.device)
    load_started = time.perf_counter()
    agent = Agent(str(model_path), device=str(requested_device), compile=False, fast=False)
    if agent.device.type != requested_device.type:
        raise RuntimeError(
            f"Laya changed device from {requested_device} to {agent.device}; restart explicitly on CPU"
        )
    thresholds = load_review_thresholds(model_path, agent)
    if agent.device.type == "mps":
        torch.mps.synchronize()
    load_ms = (time.perf_counter() - load_started) * 1000
    model_identity = {
        "path": str(model_path),
        "name": agent.cfg.get("model_name", "unknown"),
        "fine_tuned": bool(agent.cfg.get("fine_tuned", False)),
    }
    log(
        f"ready model={model_identity['name']} device={agent.device} "
        f"load_ms={load_ms:.1f} review_policy={'on' if thresholds else 'off'}"
    )
    emit(
        {
            "type": "ready",
            "protocol_version": PROTOCOL_VERSION,
            "model": model_identity,
            "device": str(agent.device),
        }
    )

    max_line_bytes = args.max_event_bytes + 64 * 1024
    while True:
        line = sys.stdin.buffer.readline(max_line_bytes + 1)
        if not line:
            break
        if len(line) > max_line_bytes:
            if not line.endswith(b"\n"):
                drain_oversized_line(sys.stdin.buffer)
            emit(
                {
                    "id": "unknown",
                    "ok": False,
                    "error": {
                        "code": "request_too_large",
                        "message": f"request exceeds {max_line_bytes} bytes",
                    },
                }
            )
            continue

        request: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            correlation_id = request_id(request.get("id"))
            event = request.get("event")
            if not isinstance(event, dict):
                raise ValueError("event must be a JSON object")
            encoded_event = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded_event) > args.max_event_bytes:
                raise ValueError(
                    f"event is {len(encoded_event)} bytes; maximum is {args.max_event_bytes}"
                )

            if agent.device.type == "mps":
                torch.mps.synchronize()
            started = time.perf_counter()
            result = agent.predict(
                event,
                ROUTING_QUESTION,
                max_len=int(config["max_length"]),
                head_max_len=int(config["head_max_length"]),
                min_confidence=thresholds,
            )
            if agent.device.type == "mps":
                torch.mps.synchronize()
            latency_ms = (time.perf_counter() - started) * 1000
            answer = result["answers"][QUESTION_ID]
            probabilities = {key: float(value) for key, value in answer["probabilities"].items()}
            if not probabilities or not all(math.isfinite(value) for value in probabilities.values()):
                raise RuntimeError("model returned non-finite probabilities")
            if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-3):
                raise RuntimeError("model probabilities do not sum to approximately one")
            emit(
                {
                    "id": correlation_id,
                    "ok": True,
                    "decision": {
                        "selected_route": answer["choice"],
                        "probabilities": probabilities,
                        "confidence": float(answer["answer_confidence"]),
                        "review_required": answer.get("abstention") == "abstained",
                        "review_status": answer.get("abstention", "not_configured"),
                        "review_threshold": answer.get("abstention_threshold"),
                        "model": model_identity,
                        "device": str(agent.device),
                        "latency_ms": round(latency_ms, 3),
                        "usage": result["usage"],
                    },
                }
            )
        except Exception as exc:
            emit(
                {
                    "id": request_id(request.get("id")) if isinstance(request, dict) else "unknown",
                    "ok": False,
                    "error": {"code": "inference_error", "message": str(exc)},
                }
            )

    log("stdin closed; worker exiting")


if __name__ == "__main__":
    main()
