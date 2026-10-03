#!/usr/bin/env python3
"""Replay exported event/topic pairs with the locally pinned Java SDKPerf."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from _shared import DATA_DIR, OUTPUT_DIR, ROOT, atomic_write_json, read_jsonl

SDKPERF_VERSION = "10.30.2"
SDKPERF_HOME = ROOT / ".cache" / "tools" / "sdkperf" / f"sdkperf-jcsmp-{SDKPERF_VERSION}" / f"sdkperf-jcsmp-{SDKPERF_VERSION}"
JAVA_HOME = ROOT / ".cache" / "tools" / "java" / "temurin-21.0.12.1" / "Contents" / "Home"
MANIFEST = DATA_DIR / "sdkperf_replay_manifest.jsonl"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="publish to the configured nonproduction broker")
    parser.add_argument("--offset", type=int, default=0, help="zero-based manifest row to start from")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--rate", type=int, default=20, help="messages/second per SDKPerf process")
    parser.add_argument("--report", type=Path, default=OUTPUT_DIR / "sdkperf_replay.json")
    return parser.parse_args()


def require_environment() -> tuple[str, str, str, str]:
    required = ("SOLACE_BROKER_URL", "SOLACE_USERNAME", "SOLACE_PASSWORD", "SOLACE_SMF_VPN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    username = os.environ["SOLACE_USERNAME"]
    vpn = os.environ["SOLACE_SMF_VPN"]
    sdkperf_user = username if "@" in username else f"{username}@{vpn}"
    return os.environ["SOLACE_BROKER_URL"], sdkperf_user, os.environ["SOLACE_PASSWORD"], vpn


def parse_sdkperf_counts(text: str) -> dict[str, int]:
    counts = {}
    patterns = {
        "published": r"(?i)(?:total\s+messages\s+transmitted|messages\s+published|total\s+messages\s+sent|num\s+msgs\s+sent)\D+(\d+)",
        "publish_acks": r"(?i)(?:total\s+ack\s+events|publish\s+acks?(?:\s+received)?|acks?\s+received)\D+(\d+)",
        "publish_nacks": r"(?i)(?:total\s+nack\s+events|publish\s+nacks?(?:\s+received)?)\D+(\d+)",
    }
    for name, pattern in patterns.items():
        matches = re.findall(pattern, text)
        if matches:
            counts[name] = int(matches[-1])
    return counts


def main() -> None:
    args = parse_args()
    if args.offset < 0 or args.offset >= 10000:
        raise SystemExit("--offset must be between 0 and 9999")
    if args.limit < 1 or args.offset + args.limit > 10000:
        raise SystemExit("--limit must be positive and offset + limit must not exceed 10000")
    if args.batch_size < 1 or args.batch_size > 250:
        raise SystemExit("--batch-size must be between 1 and 250")
    if args.rate < 1:
        raise SystemExit("--rate must be positive")
    all_records = read_jsonl(MANIFEST)
    manifest = all_records[args.offset : args.offset + args.limit]
    if len(manifest) != args.limit:
        raise RuntimeError(f"Replay manifest contains only {len(manifest)} rows")
    if len({row["correlation_id"] for row in manifest}) != len(manifest):
        raise RuntimeError("Replay manifest correlation IDs are not unique")
    for row in manifest:
        payload_path = ROOT / row["payload_path"]
        payload = payload_path.read_bytes()
        if len(payload) != row["payload_bytes"]:
            raise RuntimeError(f"Payload length changed for {row['id']}")
        if hashlib.sha256(payload).hexdigest() != row["payload_sha256"]:
            raise RuntimeError(f"Payload checksum changed for {row['id']}")
        if json.loads(payload)["topic"] != row["topic"]:
            raise RuntimeError(f"Payload/topic mismatch for {row['id']}")

    summary = {
        "sdkperf_version": SDKPERF_VERSION,
        "mode": "execute" if args.execute else "dry-run",
        "offset": args.offset,
        "records_requested": args.limit,
        "records_validated": len(manifest),
        "unique_correlations": len({row["correlation_id"] for row in manifest}),
        "unique_payloads": len({row["payload_sha256"] for row in manifest}),
        "shard_size": args.batch_size,
        "shards": (len(manifest) + args.batch_size - 1) // args.batch_size,
        "rate_per_second": args.rate,
        "delivery_mode": "persistent",
        "per_event_topic_payload_pairing": True,
        "live_broker_executed": False,
    }
    if not args.execute:
        atomic_write_json(args.report, summary)
        print(json.dumps(summary, indent=2))
        return

    broker, sdkperf_user, password, _ = require_environment()
    executable = SDKPERF_HOME / "sdkperf_java.sh"
    java = JAVA_HOME / "bin" / "java"
    if not executable.is_file() or not java.is_file():
        raise RuntimeError("Run solace-go/scripts/install-sdkperf.sh first")
    logs = ROOT / ".cache" / "sdkperf-replay" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    state_dir = ROOT / "solace-go" / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, password_path_text = tempfile.mkstemp(prefix="sdkperf-password-", dir=state_dir)
    password_path = Path(password_path_text)
    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(password)
    environment = os.environ.copy()
    environment["JAVA_HOME"] = str(JAVA_HOME)
    environment["PATH"] = f"{JAVA_HOME / 'bin'}:{environment.get('PATH', '')}"
    started = time.perf_counter()
    completed = 0
    parsed_counts = {"published": 0, "publish_acks": 0, "publish_nacks": 0}
    try:
        for shard_index, start in enumerate(range(0, len(manifest), args.batch_size)):
            shard = manifest[start : start + args.batch_size]
            payloads = ",".join(str(ROOT / row["payload_path"]) for row in shard)
            topics = ",".join(row["topic"] for row in shard)
            command = [
                str(executable),
                f"-cip={broker}",
                f"-cu={sdkperf_user}",
                f"-cpf={password_path}",
                f"-cn=edm0-sdkperf-{args.offset + start:05d}",
                f"-pal={payloads}",
                f"-ptl={topics}",
                "-mt=persistent",
                f"-mn={len(shard)}",
                f"-mr={args.rate}",
                "-apa=5000",
                "-aem=per-message",
                "-soe",
            ]
            result = subprocess.run(command, cwd=SDKPERF_HOME, env=environment, capture_output=True, text=True)
            log_path = logs / f"shard-{shard_index:03d}.log"
            log_path.write_text(result.stdout + "\n--- stderr ---\n" + result.stderr, encoding="utf-8")
            if result.returncode != 0:
                raise RuntimeError(f"SDKPerf shard {shard_index} failed; inspect {log_path}")
            shard_counts = parse_sdkperf_counts(result.stdout + "\n" + result.stderr)
            if shard_counts.get("published") != len(shard):
                raise RuntimeError(
                    f"SDKPerf shard {shard_index} reported {shard_counts.get('published')} "
                    f"transmissions, expected {len(shard)}; inspect {log_path}"
                )
            if shard_counts.get("publish_acks") != len(shard) or shard_counts.get("publish_nacks", 0) != 0:
                raise RuntimeError(
                    f"SDKPerf shard {shard_index} acknowledgement mismatch; inspect {log_path}"
                )
            completed += len(shard)
            for key, value in shard_counts.items():
                parsed_counts[key] += value
            print(f"completed shard {shard_index + 1}/{summary['shards']}: {completed}/{len(manifest)}")
    finally:
        password_path.unlink(missing_ok=True)
    summary.update(
        {
            "live_broker_executed": True,
            "records_submitted": completed,
            "sdkperf_reported_counts": parsed_counts,
            "duration_seconds": time.perf_counter() - started,
            "logs_directory": str(logs),
            "broker": "redacted",
            "username": "redacted",
        }
    )
    atomic_write_json(args.report, summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
