import json

import _shared


def test_runtime_option_order_matches_serialized_training_records(tmp_path):
    contract = {
        "question_id": "route",
        "question": {
            "type": "choice",
            "instructions": "Choose the operational owner.",
            "criteria": {"payment-operations": "payments", "finance-accounting": "ledger"},
        },
    }
    (tmp_path / "route_contract.json").write_text(json.dumps(contract))
    serialized = json.loads(json.dumps(contract, sort_keys=True))
    loaded = _shared.load_routing_question(tmp_path)
    assert list(loaded["route"]["criteria"]) == list(serialized["question"]["criteria"])
