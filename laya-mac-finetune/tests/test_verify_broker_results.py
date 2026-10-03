from __future__ import annotations

import copy
import json
import sys

import pytest

from _shared import ROUTES
from verify_broker_results import main, summarize_latency


def test_latency_summary_separates_cold_start_from_warm_percentiles():
    summary = summarize_latency([1000.0, 40.0, 50.0, 60.0, 70.0])

    assert summary == {
        "samples": 5,
        "cold_first_ms": 1000.0,
        "warm_samples": 4,
        "warm_p50_ms": 55.0,
        "warm_p95_ms": 68.5,
        "warm_p99_ms": 69.7,
        "warm_max_ms": 70.0,
        "overall_max_ms": 1000.0,
    }


def test_live_verifier_requires_zero_truncation(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    report_path = tmp_path / "report.json"
    correlation = "correlation-1"
    source_topic = "edm0/pilot/events/payments/example"
    payload_hash = "a" * 64
    manifest_path.write_text(
        json.dumps(
            {
                "correlation_id": correlation,
                "payload_sha256": payload_hash,
                "topic": source_topic,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    selected_route = ROUTES[0]
    decision = {
        "selected_route": selected_route,
        "probabilities": {route: float(route == selected_route) for route in ROUTES},
        "review_required": False,
        "latency_ms": 41.25,
        "usage": {"truncated": False, "state_tokens_dropped": 0},
    }
    publication = {
        "topic": f"edm0/pilot/results/{selected_route}",
        "payload": {
            "correlation_id": correlation,
            "source": {
                "transport": "smf",
                "delivery_semantics": "smf-persistent-guaranteed",
                "topic": source_topic,
                "payload_sha256": payload_hash,
            },
            "decision": decision,
            "bridge_latency_ms": 43.5,
        },
    }
    audit_path.write_text(json.dumps(publication) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_broker_results.py",
            "--audit",
            str(audit_path),
            "--manifest",
            str(manifest_path),
            "--expected",
            "1",
            "--report",
            str(report_path),
        ],
    )

    main()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["checks"]["zero_truncation"] is True
    assert report["latency_summary"]["decision_latency_ms"]["cold_first_ms"] == 41.25
    assert report["latency_summary"]["bridge_latency_ms"]["cold_first_ms"] == 43.5

    truncated = copy.deepcopy(publication)
    truncated["payload"]["decision"]["usage"] = {
        "truncated": True,
        "state_tokens_dropped": 3,
    }
    audit_path.write_text(json.dumps(truncated) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="1"):
        main()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["checks"]["zero_truncation"] is False
    assert report["truncation"] == {
        "truncated_decisions": 1,
        "state_tokens_dropped": 3,
    }
