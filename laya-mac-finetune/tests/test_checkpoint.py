from __future__ import annotations

import gc
import math

import pytest

from _shared import (
    BASE_MODEL_DIR,
    DATA_DIR,
    OUTPUT_DIR,
    QUESTION_ID,
    ROUTING_QUESTION,
    load_config,
    probabilities_from_answer,
    read_jsonl,
    select_device,
)


def run_one(path):
    from laya import Agent

    config = load_config()
    device = select_device(config["device"])
    agent = Agent(str(path), device=str(device), compile=False, fast=False)
    result = agent.predict(
        read_jsonl(DATA_DIR / "test.jsonl")[0]["state"],
        ROUTING_QUESTION,
        max_len=int(config["max_length"]),
        head_max_len=int(config["head_max_length"]),
    )
    answer = result["answers"][QUESTION_ID]
    assert answer["type"] == "choice"
    assert answer["choice"] in ROUTING_QUESTION[QUESTION_ID]["criteria"]
    probabilities = probabilities_from_answer(answer)
    assert all(math.isfinite(value) for value in probabilities)
    assert sum(probabilities) == pytest.approx(1.0, abs=1e-6)
    agent.model = None
    del agent
    gc.collect()
    return set(answer["probabilities"])


@pytest.mark.skipif(not BASE_MODEL_DIR.exists(), reason="base model has not been downloaded")
def test_base_model_runs_native_choice_inference():
    assert run_one(BASE_MODEL_DIR) == set(ROUTING_QUESTION[QUESTION_ID]["criteria"])


@pytest.mark.skipif(
    not (OUTPUT_DIR / "best-model").exists(), reason="fine-tuned checkpoint has not been created"
)
def test_saved_checkpoint_loads_and_uses_same_contract():
    base_contract = run_one(BASE_MODEL_DIR)
    fine_contract = run_one(OUTPUT_DIR / "best-model")
    assert fine_contract == base_contract
