"""Frozen features must follow inputs and weights, independently of labels."""
import copy

import train
import pytest


def test_cache_invalidates_changed_tokens_with_same_record_id(tmp_path, monkeypatch):
    monkeypatch.setattr(train, "BASE_MODEL_DIR", tmp_path)
    (tmp_path / "model.safetensors").write_bytes(b"base weights")
    config = {"max_len": 384, "encoder": "test"}
    item = {"id": "unchanged-id", "ids": [1, 2, 3], "label": 0}
    original = train.encoder_cache_key([item], config)
    changed = copy.deepcopy(item)
    changed["ids"][1] = 9
    assert train.encoder_cache_key([changed], config) != original
    changed = copy.deepcopy(item)
    changed["label"] = 1
    assert train.encoder_cache_key([changed], config) == original


def test_cache_invalidates_changed_encoder_weights(tmp_path, monkeypatch):
    monkeypatch.setattr(train, "BASE_MODEL_DIR", tmp_path)
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(b"first weights")
    config = {"max_len": 384, "encoder": "test"}
    item = {"id": "record", "ids": [1, 2]}
    original = train.encoder_cache_key([item], config)
    weights.write_bytes(b"replacement weights")
    assert train.encoder_cache_key([item], config) != original


def test_training_lock_blocks_duplicate_start_and_releases(tmp_path, monkeypatch):
    monkeypatch.setattr(train, "OUTPUT_DIR", tmp_path)
    with train.exclusive_training():
        with pytest.raises(SystemExit, match="Another training run is active"):
            with train.exclusive_training():
                pytest.fail("duplicate training acquired the cache lock")
    with train.exclusive_training():
        pass
