from __future__ import annotations

import hashlib
import json

from _shared import DATA_DIR, read_jsonl
from run_sdkperf_replay import parse_sdkperf_counts


def test_sdkperf_manifest_has_unique_payload_topic_pairs():
    rows = read_jsonl(DATA_DIR / "sdkperf_replay_manifest.jsonl")
    assert len(rows) == 10000
    assert len({row["correlation_id"] for row in rows}) == 10000
    assert len({row["payload_sha256"] for row in rows}) == 10000
    source = {
        record["id"]: record["state"]
        for split in ("train", "validation", "test")
        for record in read_jsonl(DATA_DIR / f"{split}.jsonl")
    }
    for row in (rows[0], rows[len(rows) // 2], rows[-1]):
        payload = json.dumps(
            source[row["id"]], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        assert source[row["id"]]["topic"] == row["topic"]
        assert hashlib.sha256(payload).hexdigest() == row["payload_sha256"]
        assert row["topic"].startswith("edm0/pilot/events/")


def test_sdkperf_output_count_parser_matches_local_help_format():
    output = """
    Total Messages transmitted = 250
    AD Pub ACK/NACK stats:
      Total ACK Events     = 250
      Total NACK Events    = 0
    """
    assert parse_sdkperf_counts(output) == {
        "published": 250,
        "publish_acks": 250,
        "publish_nacks": 0,
    }
