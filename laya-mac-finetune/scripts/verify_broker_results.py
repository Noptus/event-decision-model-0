#!/usr/bin/env python3
"""Stream-verify confirmed broker result audits against the SDKPerf replay manifest."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from _shared import DATA_DIR, OUTPUT_DIR, ROUTES, atomic_write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, action="append", required=True)
    parser.add_argument("--manifest", type=Path, default=DATA_DIR / "sdkperf_replay_manifest.jsonl")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--expected", type=int, default=10000)
    parser.add_argument("--output-prefix", default="edm0/pilot/results/")
    parser.add_argument("--report", type=Path, default=OUTPUT_DIR / "smf_live_result.json")
    return parser.parse_args()


def load_expected(path: Path, offset: int, count: int) -> dict[str, dict[str, Any]]:
    expected = {}
    selected_index = 0
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            if line_number <= offset:
                continue
            if selected_index >= count:
                break
            row = json.loads(line)
            correlation = row["correlation_id"]
            if correlation in expected:
                raise ValueError(f"duplicate manifest correlation at line {line_number}")
            expected[correlation] = row
            selected_index += 1
    return expected


def main() -> None:
    args = parse_args()
    if args.offset < 0 or args.expected < 1:
        raise SystemExit("--offset must be nonnegative and --expected positive")
    expected = load_expected(args.manifest, args.offset, args.expected)
    if len(expected) != args.expected:
        raise RuntimeError(
            f"manifest contains {len(expected)} rows in requested range; expected {args.expected}"
        )
    occurrences = Counter()
    successful = 0
    error_envelopes = 0
    invalid = []
    review = 0
    routes = Counter()
    transport_counts = Counter()
    total_lines = 0

    for audit_path in args.audit:
        with audit_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                total_lines += 1
                try:
                    publication = json.loads(line)
                    topic = publication["topic"]
                    envelope = publication["payload"]
                    correlation = envelope["correlation_id"]
                    source = envelope["source"]
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    invalid.append(f"{audit_path}:{line_number}: malformed audit record: {exc}")
                    continue
                occurrences[correlation] += 1
                expected_row = expected.get(correlation)
                if expected_row is None:
                    invalid.append(f"{audit_path}:{line_number}: unknown correlation {correlation}")
                    continue
                if source.get("payload_sha256") != expected_row["payload_sha256"]:
                    invalid.append(f"{audit_path}:{line_number}: payload hash mismatch for {correlation}")
                if source.get("topic") != expected_row["topic"]:
                    invalid.append(f"{audit_path}:{line_number}: source topic mismatch for {correlation}")
                if source.get("transport") != "smf" or source.get("delivery_semantics") != "smf-persistent-guaranteed":
                    invalid.append(f"{audit_path}:{line_number}: incorrect transport metadata for {correlation}")

                if envelope.get("error") is not None:
                    error_envelopes += 1
                    continue
                decision = envelope.get("decision")
                if not isinstance(decision, dict):
                    invalid.append(f"{audit_path}:{line_number}: missing decision for {correlation}")
                    continue
                probabilities = decision.get("probabilities")
                if not isinstance(probabilities, dict) or set(probabilities) != set(ROUTES):
                    invalid.append(f"{audit_path}:{line_number}: invalid route keys for {correlation}")
                    continue
                values = [float(probabilities[route]) for route in ROUTES]
                if not all(math.isfinite(value) and 0 <= value <= 1 for value in values):
                    invalid.append(f"{audit_path}:{line_number}: non-finite probability for {correlation}")
                    continue
                if not math.isclose(sum(values), 1.0, abs_tol=1e-3):
                    invalid.append(f"{audit_path}:{line_number}: probabilities do not sum to one for {correlation}")
                    continue
                route = decision.get("selected_route")
                if route not in ROUTES:
                    invalid.append(f"{audit_path}:{line_number}: unknown selected route for {correlation}")
                    continue
                expected_suffix = "review" if decision.get("review_required") else route
                if topic != args.output_prefix + expected_suffix:
                    invalid.append(f"{audit_path}:{line_number}: output topic does not match decision policy for {correlation}")
                    continue
                usage = decision.get("usage", {})
                if not isinstance(usage.get("truncated"), bool) or not isinstance(usage.get("state_tokens_dropped"), int):
                    invalid.append(f"{audit_path}:{line_number}: missing native usage metadata for {correlation}")
                    continue
                successful += 1
                review += int(bool(decision.get("review_required")))
                routes[route] += 1

    duplicates = sum(count - 1 for count in occurrences.values() if count > 1)
    missing = sorted(set(expected) - set(occurrences))
    report = {
        "expected": args.expected,
        "audit_lines": total_lines,
        "unique_correlations": len(occurrences),
        "duplicate_outputs": duplicates,
        "missing_correlations": len(missing),
        "successful_decisions": successful,
        "error_envelopes": error_envelopes,
        "invalid_records": len(invalid),
        "review_required": review,
        "routes": dict(routes),
        "checks": {
            "exact_expected_unique_correlations": len(occurrences) == args.expected,
            "all_expected_correlations_seen": not missing and len(expected) == args.expected,
            "all_outputs_valid_successful_decisions": successful == total_lines and not invalid,
            "no_duplicate_outputs": duplicates == 0,
        },
        "invalid_examples": invalid[:20],
        "missing_correlation_examples": missing[:20],
    }
    atomic_write_json(args.report, report)
    print(json.dumps(report, indent=2))
    required_checks = (
        report["checks"]["exact_expected_unique_correlations"],
        report["checks"]["all_expected_correlations_seen"],
        report["checks"]["all_outputs_valid_successful_decisions"],
    )
    if not all(required_checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
