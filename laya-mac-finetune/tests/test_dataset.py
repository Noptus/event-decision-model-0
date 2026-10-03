from __future__ import annotations

import json
import math
from collections import Counter

import pytest

from _shared import DATA_DIR, QUESTION_ID, ROUTES, ROUTING_QUESTION, read_jsonl, state_fingerprint
from validate_dataset import EXPECTED, REQUIRED_STATE_FIELDS, validate


@pytest.fixture(scope="module")
def splits():
    return {name: read_jsonl(DATA_DIR / f"{name}.jsonl") for name in EXPECTED}


def test_dataset_schema_and_counts(splits):
    audit = validate()
    assert {name: details["count"] for name, details in audit["splits"].items()} == EXPECTED
    for records in splits.values():
        for record in records:
            assert set(record["state"]) == REQUIRED_STATE_FIELDS
            assert record["questions"] == ROUTING_QUESTION
            target = record["gold"][QUESTION_ID]["probabilities"]
            assert set(target) == set(ROUTES)
            assert math.isclose(sum(target.values()), 1.0, abs_tol=1e-9)


def test_no_exact_state_leakage(splits):
    owners = {}
    for split, records in splits.items():
        for record in records:
            digest = state_fingerprint(record["state"])
            assert digest not in owners, (split, record["id"], owners.get(digest))
            owners[digest] = (split, record["id"])


def test_every_route_and_language_are_represented(splits):
    for split, records in splits.items():
        labels = Counter(record["gold"][QUESTION_ID]["label"] for record in records)
        assert set(labels) == set(ROUTES), split
        assert {record["language"] for record in records} == {"en", "fr"}


def test_each_actual_scenario_is_bilingual(splits):
    for split, records in splits.items():
        coverage = {}
        for record in records:
            key = (record["gold"][QUESTION_ID]["label"], record["scenario_family"])
            coverage.setdefault(key, set()).add(record["language"])
        assert all(languages == {"en", "fr"} for languages in coverage.values()), split


def test_test_split_has_unseen_topic_pattern_for_each_route(splits):
    represented = {
        record["gold"][QUESTION_ID]["label"]
        for record in splits["test"]
        if "unseen_topic_pattern" in record["challenge_tags"]
    }
    assert represented == set(ROUTES)


def test_state_has_no_route_label_leakage(splits):
    for records in splits.values():
        for record in records:
            serialized = json.dumps(record["state"], ensure_ascii=False).lower()
            assert record["gold"][QUESTION_ID]["label"] not in serialized
