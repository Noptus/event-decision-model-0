#!/usr/bin/env python3
"""Validate schema, balance, label isolation, and split isolation."""
from __future__ import annotations

import math
from collections import Counter, defaultdict

from _shared import DATA_DIR, QUESTION_ID, ROUTES, ROUTING_QUESTION, read_jsonl, state_fingerprint

EXPECTED = {"train": 160, "validation": 40, "test": 60}
REQUIRED_STATE_FIELDS = {"topic", "schema_name", "schema_version", "event_type", "payload"}
FORBIDDEN_STATE_KEYS = {"route", "label", "gold", "expected_route", "target"}


def nested_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_keys(child)


def validate() -> dict:
    fingerprints: dict[str, str] = {}
    summary = {}
    for split, expected_count in EXPECTED.items():
        records = read_jsonl(DATA_DIR / f"{split}.jsonl")
        assert len(records) == expected_count, (split, len(records), expected_count)
        labels = Counter()
        languages = Counter()
        unseen_by_route = Counter()
        variant_languages = defaultdict(set)
        for record in records:
            assert record["split"] == split
            assert record["questions"] == ROUTING_QUESTION
            assert set(record["state"]) == REQUIRED_STATE_FIELDS
            assert not (set(nested_keys(record["state"])) & FORBIDDEN_STATE_KEYS)
            assert isinstance(record["state"]["payload"], dict)
            assert record["language"] in {"en", "fr"}
            gold = record["gold"][QUESTION_ID]
            label = gold["label"]
            probabilities = gold["probabilities"]
            assert label in ROUTES
            assert set(probabilities) == set(ROUTES)
            assert all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in probabilities.values())
            assert math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-9)
            assert max(probabilities, key=probabilities.get) == label
            labels[label] += 1
            languages[record["language"]] += 1
            variant_languages[(label, record["state"]["event_type"])].add(record["language"])
            if "unseen_topic_pattern" in record.get("challenge_tags", []):
                unseen_by_route[label] += 1
            fingerprint = state_fingerprint(record["state"])
            assert fingerprint not in fingerprints, (
                f"exact state leakage: {record['id']} duplicates {fingerprints[fingerprint]}"
            )
            fingerprints[fingerprint] = record["id"]
        assert set(labels) == set(ROUTES)
        assert set(languages) == {"en", "fr"}
        assert len(variant_languages) == len(ROUTES) * 3
        assert all(value == {"en", "fr"} for value in variant_languages.values()), variant_languages
        if split == "test":
            assert set(unseen_by_route) == set(ROUTES), unseen_by_route
        summary[split] = {
            "count": len(records),
            "routes": dict(labels),
            "languages": dict(languages),
            "unseen_topic_patterns": dict(unseen_by_route),
        }
    return summary


def main() -> None:
    summary = validate()
    for split, details in summary.items():
        print(f"{split}: {details}")
    print(f"Validated {sum(EXPECTED.values())} unique states with no exact split leakage.")


if __name__ == "__main__":
    main()
