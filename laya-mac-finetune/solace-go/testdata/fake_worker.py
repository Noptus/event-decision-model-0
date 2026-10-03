#!/usr/bin/env python3
"""Tiny subprocess fixture for Go worker lifecycle tests."""
import argparse
import json
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--model", required=True)
parser.add_argument("--device")
parser.add_argument("--max-event-bytes")
args = parser.parse_args()

if args.model == "no-ready":
    time.sleep(60)
    raise SystemExit(0)

print(json.dumps({"type": "ready", "protocol_version": 1}), flush=True)
if args.model == "exit-after-ready":
    raise SystemExit(0)
if args.model == "blocked-write":
    time.sleep(60)
    raise SystemExit(0)
if args.model == "extra-output":
    for index in range(20):
        print(json.dumps({"id": str(index), "ok": False, "error": {"code": "extra", "message": "extra"}}), flush=True)
    time.sleep(60)
    raise SystemExit(0)

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({
        "id": request["id"],
        "ok": True,
        "decision": {
            "selected_route": "logistics",
            "probabilities": {"logistics": 1.0},
            "confidence": 1.0,
            "review_required": False,
            "review_status": "not_configured",
            "review_threshold": None,
            "model": {"path": "fake", "name": "fake", "fine_tuned": False},
            "device": "cpu",
            "latency_ms": 0.1
        }
    }), flush=True)
