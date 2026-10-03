from __future__ import annotations

import json

import pytest

from infer import review_thresholds
from _shared import question_contract_hash


class DummyAgent:
    def __init__(self, config):
        self.cfg = config


def test_custom_checkpoint_does_not_borrow_global_policy(tmp_path):
    agent = DummyAgent({"fine_tuned": True, "calibration_id": "expected"})
    assert review_thresholds(tmp_path, agent, disabled=False) is None


def test_checkpoint_policy_requires_matching_identity(tmp_path):
    (tmp_path / "calibration.json").write_text(
        json.dumps(
            {
                "calibration_id": "other",
                "checkpoint_identity": {"question_contract_sha256": question_contract_hash()},
                "abstention_thresholds": {"choice:6-10": 0.7},
            }
        ),
        encoding="utf-8",
    )
    agent = DummyAgent({"fine_tuned": True, "calibration_id": "expected"})
    with pytest.raises(RuntimeError, match="does not match"):
        review_thresholds(tmp_path, agent, disabled=False)


def test_checkpoint_policy_loads_only_when_bound(tmp_path):
    calibration_id = "bound-id"
    (tmp_path / "calibration.json").write_text(
        json.dumps(
            {
                "calibration_id": calibration_id,
                "checkpoint_identity": {"question_contract_sha256": question_contract_hash()},
                "abstention_thresholds": {"choice:6-10": 0.7},
            }
        ),
        encoding="utf-8",
    )
    agent = DummyAgent({"fine_tuned": True, "calibration_id": calibration_id})
    assert review_thresholds(tmp_path, agent, disabled=False) == {"choice:6-10": 0.7}
