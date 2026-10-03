#!/usr/bin/env python3
"""Inspect this Mac and persist reproducibility metadata."""
from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from _shared import CONFIG_PATH, ROOT, load_config, update_manifest


def command(*args: str) -> str | None:
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def integer_command(*args: str) -> int | None:
    value = command(*args)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def package_versions() -> dict[str, str]:
    versions = {}
    for name in ("laya", "torch", "transformers", "safetensors", "huggingface-hub", "numpy", "pyyaml"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def main() -> None:
    config = load_config(CONFIG_PATH)
    total_memory = integer_command("sysctl", "-n", "hw.memsize")
    chip = command("sysctl", "-n", "machdep.cpu.brand_string") or platform.processor() or "unknown"
    model = command("sysctl", "-n", "hw.model") or "unknown"
    macos = command("sw_vers", "-productVersion") or platform.mac_ver()[0]
    free_bytes = shutil.disk_usage(ROOT).free

    torch_info: dict[str, object]
    try:
        import torch

        built = bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_built())
        available = bool(built and torch.backends.mps.is_available())
        probe_ok = False
        probe_error = None
        if available:
            try:
                probe_ok = float((torch.ones(1, device="mps") + 1).cpu().item()) == 2.0
            except RuntimeError as exc:
                probe_error = str(exc)
        torch_info = {
            "version": torch.__version__,
            "mps_built": built,
            "mps_available": available,
            "mps_tensor_probe": probe_ok,
            "mps_probe_error": probe_error,
        }
    except ImportError:
        torch_info = {"version": "not-installed", "mps_built": False, "mps_available": False}

    memory_gib = round(total_memory / (1024**3), 2) if total_memory else None
    if memory_gib is not None and memory_gib <= 8:
        recommendation = {"freeze_strategy": "head_only", "max_length": 192, "max_steps": 30}
    elif memory_gib is not None and memory_gib <= 16:
        recommendation = {"freeze_strategy": "head_only", "max_length": 256, "max_steps": 60}
    elif memory_gib is not None and memory_gib <= 24:
        recommendation = {"freeze_strategy": "last_1", "max_length": 256, "max_steps": 100}
    else:
        recommendation = {"freeze_strategy": "last_2", "max_length": 384, "max_steps": 150}

    laya_sha = command("git", "-C", str(ROOT / "upstream" / "laya"), "rev-parse", "HEAD")
    report = {
        "chip": chip,
        "hardware_model": model,
        "unified_memory_gib": memory_gib,
        "macos": macos,
        "architecture": platform.machine(),
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "free_disk_gib": round(free_bytes / (1024**3), 2),
        "torch": torch_info,
        "recommended_settings": recommendation,
    }
    minimum = float(config["min_free_disk_gb"])
    if free_bytes < minimum * (1024**3):
        raise SystemExit(
            f"Refusing to proceed: {report['free_disk_gib']} GiB free, {minimum:.1f} GiB required."
        )

    update_manifest(
        system=report,
        packages=package_versions(),
        source={
            "laya_repository": config["laya_repository"],
            "laya_revision": laya_sha,
            "expected_laya_revision": config["laya_revision"],
            "base_model": config["base_model"],
            "model_revision": config["model_revision"],
        },
        environment={
            key: os.environ.get(key)
            for key in (
                "PYTORCH_ENABLE_MPS_FALLBACK",
                "USE_TF",
                "HF_HOME",
                "HF_HUB_CACHE",
                "TORCH_HOME",
                "UV_CACHE_DIR",
            )
        },
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
