#!/usr/bin/env python3
"""Download the pinned public Laya checkpoint into the project."""
from __future__ import annotations

import json
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from _shared import BASE_MODEL_DIR, CONFIG_PATH, load_config, update_manifest

REQUIRED = (
    "rl_agent_config.json",
    "model.safetensors",
    "encoder/config.json",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
)


def main() -> None:
    config = load_config(CONFIG_PATH)
    model_id = str(config["base_model"])
    revision = str(config["model_revision"])
    BASE_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {model_id}@{revision[:12]} to {BASE_MODEL_DIR}")
    snapshot_download(
        repo_id=model_id,
        revision=revision,
        local_dir=BASE_MODEL_DIR,
        token=False,
        allow_patterns=[
            "rl_agent_config.json",
            "model.safetensors",
            "encoder/*",
            "tokenizer/*",
            "README.md",
        ],
    )
    missing = [name for name in REQUIRED if not (BASE_MODEL_DIR / name).is_file()]
    if missing:
        raise RuntimeError(f"Downloaded checkpoint is incomplete; missing: {missing}")

    info = HfApi(token=False).model_info(model_id, revision=revision, files_metadata=True)
    resolved_revision = str(info.sha)
    if resolved_revision != revision:
        raise RuntimeError(f"Model revision mismatch: expected {revision}, resolved {resolved_revision}")
    files = {
        name: (BASE_MODEL_DIR / name).stat().st_size
        for name in REQUIRED
        if (BASE_MODEL_DIR / name).exists()
    }
    with (BASE_MODEL_DIR / "rl_agent_config.json").open(encoding="utf-8") as handle:
        model_config = json.load(handle)
    update_manifest(
        model={
            "id": model_id,
            "requested_revision": revision,
            "resolved_revision": resolved_revision,
            "local_path": str(BASE_MODEL_DIR),
            "files_bytes": files,
            "checkpoint_config": model_config,
        }
    )
    print(f"Verified revision {resolved_revision}")
    print(f"Weights: {files['model.safetensors'] / (1024**2):.1f} MiB")


if __name__ == "__main__":
    main()
