from __future__ import annotations

import pytest

from train import validate_exposure_plan


def test_full_pass_plan_is_accepted():
    assert validate_exposure_plan("experiment", 250, 16, 2, 8000, 1) == (8000, 8000)


def test_partial_pass_plan_is_rejected():
    with pytest.raises(ValueError, match="do not cover"):
        validate_exposure_plan("experiment", 250, 8, 2, 8000, 1)


def test_smoke_plan_can_be_partial():
    assert validate_exposure_plan("smoke", 2, 1, 1, 64, 1) == (2, 64)
