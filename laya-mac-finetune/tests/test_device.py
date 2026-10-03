from __future__ import annotations

import torch

from _shared import select_device


def test_explicit_cpu_selection():
    assert select_device("cpu").type == "cpu"


def test_auto_selection_matches_usable_backend():
    selected = select_device("auto")
    assert selected.type in {"mps", "cpu"}
    if selected.type == "mps":
        assert torch.backends.mps.is_available()
        assert float((torch.ones(1, device=selected) + 1).cpu().item()) == 2.0
