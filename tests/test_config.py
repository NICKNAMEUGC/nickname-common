"""Tests para nickname_common.config"""

import os

import pytest

from nickname_common.config import load_config


def test_load_config_returns_dict():
    config = load_config()
    assert isinstance(config, dict)


def test_load_config_optional_defaults():
    config = load_config(optional={"TEST_VAR_XYZ": "default_value"})
    assert config["TEST_VAR_XYZ"] == "default_value"


def test_load_config_required_missing_raises():
    """Falta una variable required → ValueError."""
    with pytest.raises(ValueError, match="obligatorias"):
        load_config(required=["NONEXISTENT_VAR_XYZ_123"])


def test_load_config_required_present():
    """Variable required que sí existe."""
    os.environ["_TEST_COMMON_VAR"] = "test_value"
    try:
        config = load_config(required=["_TEST_COMMON_VAR"])
        assert config["_TEST_COMMON_VAR"] == "test_value"
    finally:
        del os.environ["_TEST_COMMON_VAR"]
