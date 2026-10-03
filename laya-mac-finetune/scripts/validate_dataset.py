#!/usr/bin/env python3
"""Audit the 10k dataset for balance, leakage, semantics, and native tokenization."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from generate_dataset import DOMAIN_ROUTE_UNITS, OOD_SCENARIOS, ROUTE_TOPIC, SPLIT_SCALE
from _shared import (
    BASE_MODEL_DIR,
    CONFIG_PATH,
    DATA_DIR,
    OUTPUT_DIR,
    QUESTION_ID,
    ROUTES,
    ROUTING_QUESTION,
    atomic_write_json,
    load_config,
    read_jsonl,
    state_fingerprint,
)

EXPECTED = {split: scale * 40 for split, scale in SPLIT_SCALE.items()}
EXPECTED_PER_ROUTE = {split: scale * 5 for split, scale in SPLIT_SCALE.items()}
EXPECTED_PER_DOMAIN = EXPECTED_PER_ROUTE
EXPECTED_DOMAINS = set(DOMAIN_ROUTE_UNITS)
REQUIRED_STATE_FIELDS = {
    "topic",
    "schema_name",
    "schema_version",
    "event_type",
    "event_timestamp",
    "source_system",
    "correlation_id",
    "entity",
    "payload",
}
FORBIDDEN_STATE_KEYS = {
    "route",
    "label",
    "gold",
    "target",
    "rationale",
    "manual_review",
    "review_needed",
    "scenario_family",
    "split",
}


def nested_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_keys(child)


def semantic_fingerprint(record: dict[str, Any]) -> str:
    state = record["state"]
    descriptor = {
        "schema_name": state["schema_name"],
        "event_type": state["event_type"],
        "payload_keys": sorted(
            key for key in state["payload"] if key not in {"event_id"}
        ),
    }
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalized_pair_state(record: dict[str, Any]) -> dict[str, Any]:
    state = copy.deepcopy(record["state"])
    state.pop("correlation_id", None)
    state.pop("event_timestamp", None)
    state.get("payload", {}).pop("event_id", None)
    changed = record["annotation"]["changed_field"].split(".", 1)[1]
    state["payload"].pop(changed, None)
    return state


def validate(tokenization: bool = False) -> dict[str, Any]:
    fingerprints: dict[str, tuple[str, str]] = {}
    entities: dict[str, str] = {}
    semantic_owners: dict[str, str] = {}
    scenario_owners: dict[str, str] = {}
    pair_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    summary: dict[str, Any] = {}
    all_records: list[dict[str, Any]] = []
    ood_signatures = {
        (schema, event_type)
        for scenarios in OOD_SCENARIOS.values()
        for _, schema, event_type, _ in scenarios
    }

    for split, expected_count in EXPECTED.items():
        records = read_jsonl(DATA_DIR / f"{split}.jsonl")
        assert len(records) == expected_count, (split, len(records), expected_count)
        labels = Counter()
        domains = Counter()
        languages = Counter()
        challenges = Counter()
        scenario_languages: dict[tuple[str, str], set[str]] = defaultdict(set)
        versions_by_route_language: dict[tuple[str, str], set[str]] = defaultdict(set)
        timestamps = []
        for record in records:
            assert record["split"] == split
            assert record["questions"] == ROUTING_QUESTION
            assert set(record["state"]) == REQUIRED_STATE_FIELDS
            assert not (set(nested_keys(record["state"])) & FORBIDDEN_STATE_KEYS)
            assert isinstance(record["state"]["payload"], dict)
            if record["generation"]["kind"] == "ood":
                assert record["state"]["entity"]["type"] == "observation", "unknown-owner events must not encode a provisional route in entity type"
            assert record["language"] in {"en", "fr"}
            assert record["domain"] in EXPECTED_DOMAINS
            timestamp = datetime.fromisoformat(record["state"]["event_timestamp"].replace("Z", "+00:00"))
            timestamps.append(timestamp)

            gold = record["gold"][QUESTION_ID]
            label = gold["label"]
            probabilities = gold["probabilities"]
            assert label in ROUTES
            assert set(probabilities) == set(ROUTES)
            assert all(math.isfinite(value) and 0 <= value <= 1 for value in probabilities.values())
            assert math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-9)
            assert max(probabilities, key=probabilities.get) == label
            serialized_state = json.dumps(record["state"], ensure_ascii=False).lower()
            assert label not in serialized_state
            assert record["annotation"]["rationale"] not in serialized_state

            labels[label] += 1
            domains[record["domain"]] += 1
            languages[record["language"]] += 1
            scenario_languages[(label, record["scenario_family"])].add(record["language"])
            versions_by_route_language[(label, record["language"])].add(record["state"]["schema_version"])
            challenges.update(record["challenge_tags"])

            digest = state_fingerprint(record["state"])
            assert digest not in fingerprints, f"exact state leakage: {record['id']} duplicates {fingerprints.get(digest)}"
            fingerprints[digest] = (split, record["id"])
            entity = record["entity_group"]
            assert entity not in entities or entities[entity] == split, f"entity leakage: {entity}"
            entities[entity] = split
            family = record["scenario_family"]
            assert family not in scenario_owners or scenario_owners[family] == split, f"scenario-family leakage: {family}"
            scenario_owners[family] = split
            semantic = semantic_fingerprint(record)
            assert semantic not in semantic_owners or semantic_owners[semantic] == split, (
                f"semantic-template leakage: {record['id']} matches {semantic_owners.get(semantic)}"
            )
            semantic_owners[semantic] = split

            pair_id = record["annotation"].get("counterfactual_pair_id")
            if pair_id:
                pair_records[pair_id].append(record)
            if "topic_payload_conflict" in record["challenge_tags"]:
                secondary = record["annotation"]["secondary_route"]
                assert f"/{ROUTE_TOPIC[secondary]}/" in record["state"]["topic"]
                assert f"/{ROUTE_TOPIC[label]}/" not in record["state"]["topic"]
            if "out_of_distribution" in record["challenge_tags"]:
                assert (record["state"]["schema_name"], record["state"]["event_type"]) in ood_signatures
                assert record["annotation"]["review_needed"] is True
                assert max(probabilities.values()) <= 0.22 + 1e-9

        assert labels == Counter({route: EXPECTED_PER_ROUTE[split] for route in ROUTES})
        assert domains == Counter({domain: EXPECTED_PER_DOMAIN[split] for domain in EXPECTED_DOMAINS})
        assert abs(languages["en"] - languages["fr"]) <= 4, languages
        assert all(value == {"en", "fr"} for value in scenario_languages.values()), scenario_languages
        assert all(value == {"1.0", "2.0", "3.0"} for value in versions_by_route_language.values()), versions_by_route_language
        summary[split] = {
            "count": len(records),
            "routes": dict(labels),
            "domains": dict(domains),
            "languages": dict(languages),
            "challenges": dict(challenges),
            "timestamp_min": min(timestamps).isoformat(),
            "timestamp_max": max(timestamps).isoformat(),
            "scenario_families": len({record["scenario_family"] for record in records}),
        }
        all_records.extend(records)

    assert summary["train"]["timestamp_max"] < summary["validation"]["timestamp_min"]
    assert summary["validation"]["timestamp_max"] < summary["test"]["timestamp_min"]
    assert pair_records
    for pair_id, records in pair_records.items():
        assert len(records) == 2, (pair_id, len(records))
        assert len({record["gold"][QUESTION_ID]["label"] for record in records}) == 2
        assert records[0]["annotation"]["changed_field"] == records[1]["annotation"]["changed_field"]
        assert normalized_pair_state(records[0]) == normalized_pair_state(records[1]), pair_id

    audit: dict[str, Any] = {
        "dataset_version": "edm0-enterprise-v2",
        "total": len(all_records),
        "splits": summary,
        "unique_states": len(fingerprints),
        "unique_entity_groups": len(entities),
        "counterfactual_pairs": len(pair_records),
        "checks": {
            "schema_valid": True,
            "exact_duplicates_across_splits": 0,
            "entity_overlap_across_splits": 0,
            "scenario_family_overlap_across_splits": 0,
            "semantic_template_overlap_across_splits": 0,
            "route_label_or_annotation_in_model_state": False,
            "time_ranges_disjoint": True,
            "counterfactual_pairs_valid": True,
            "topic_conflicts_semantically_verified": True,
            "language_varies_within_scenario_and_schema_version": True,
        },
    }
    if tokenization:
        audit["tokenization"] = audit_tokenization(all_records)
    atomic_write_json(OUTPUT_DIR / "dataset_audit.json", audit)
    return audit


def audit_tokenization(records: list[dict[str, Any]]) -> dict[str, Any]:
    from transformers import AutoTokenizer
    from laya.common import build_sequence, render_options

    config = load_config(CONFIG_PATH)
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_DIR / "tokenizer", local_files_only=True)
    max_length = int(config["max_length"])
    head_max_length = int(config["head_max_length"])
    truncated = 0
    max_state_tokens = 0
    max_input_tokens = 0
    marker_failures = []
    for record in records:
        question = record["questions"][QUESTION_ID]
        internal = {"t": question["type"], "ins": question["instructions"], "crit": question["criteria"]}
        sequence, markers, _, state_stats = build_sequence(
            tokenizer,
            record["state"],
            internal,
            max_length,
            head_max_length,
            return_stats=True,
            return_truncation_stats=True,
        )
        max_input_tokens = max(max_input_tokens, len(sequence))
        max_state_tokens = max(max_state_tokens, int(state_stats["state_tokens"]))
        truncated += int(state_stats["truncated"])
        if len(markers) != len(render_options(internal)):
            marker_failures.append(record["id"])
    assert not marker_failures, marker_failures[:10]
    assert truncated == 0, f"{truncated} events would lose state tokens"
    return {
        "max_length": max_length,
        "head_max_length": head_max_length,
        "max_encoded_tokens": max_input_tokens,
        "max_state_tokens": max_state_tokens,
        "truncated_events": truncated,
        "option_marker_failures": len(marker_failures),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenization", action="store_true")
    args = parser.parse_args()
    print(json.dumps(validate(tokenization=args.tokenization), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
