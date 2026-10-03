#!/usr/bin/env python3
"""Export each synthetic event as a distinct SDKPerf payload/topic pair."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from _shared import DATA_DIR, OUTPUT_DIR, ROOT, atomic_write_json, read_jsonl, write_jsonl

EXPORT_ROOT = ROOT / ".cache" / "sdkperf-replay"
PAYLOAD_DIR = EXPORT_ROOT / "payloads"
MANIFEST_PATH = DATA_DIR / "sdkperf_replay_manifest.jsonl"
PLAN_PATH = OUTPUT_DIR / "sdkperf_replay_plan.json"
SHARD_SIZE = 100


def main() -> None:
    if PAYLOAD_DIR.exists():
        shutil.rmtree(PAYLOAD_DIR)
    PAYLOAD_DIR.mkdir(parents=True)
    manifest = []
    sequence = 0
    for split in ("train", "validation", "test"):
        for record in read_jsonl(DATA_DIR / f"{split}.jsonl"):
            sequence += 1
            payload = json.dumps(record["state"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            path = PAYLOAD_DIR / f"{sequence:05d}-{record['id']}.json"
            path.write_bytes(payload)
            manifest.append(
                {
                    "sequence": sequence,
                    "id": record["id"],
                    "split": split,
                    "topic": record["state"]["topic"],
                    "correlation_id": record["state"]["correlation_id"],
                    "payload_path": str(path.relative_to(ROOT)),
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "payload_bytes": len(payload),
                }
            )
    if sequence != 10000:
        raise RuntimeError(f"Expected exactly 10000 exports, got {sequence}")
    if len({row["correlation_id"] for row in manifest}) != sequence:
        raise RuntimeError("SDKPerf export correlation IDs are not unique")
    if len({row["payload_sha256"] for row in manifest}) != sequence:
        raise RuntimeError("SDKPerf export payloads are not unique")
    for row in manifest:
        if json.loads((ROOT / row["payload_path"]).read_text()) ["topic"] != row["topic"]:
            raise RuntimeError(f"Payload/topic mismatch for {row['id']}")
    write_jsonl(MANIFEST_PATH, manifest)
    plan = {
        "tool": "Solace SDKPerf Java",
        "version": "10.30.2",
        "records": sequence,
        "shard_size": SHARD_SIZE,
        "shards": (sequence + SHARD_SIZE - 1) // SHARD_SIZE,
        "unique_correlation_ids": sequence,
        "unique_payloads": sequence,
        "unique_topics": len({row["topic"] for row in manifest}),
        "payload_directory": str(PAYLOAD_DIR),
        "manifest": str(MANIFEST_PATH),
        "note": "SDKPerf replays generated records; it does not create or label business semantics.",
    }
    atomic_write_json(PLAN_PATH, plan)
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
