"""Tests del registro central de modelos (F0.1)."""

import pytest

from nickname_common import llm


def test_get_model_defaults():
    assert llm.get_model("gemini_flash") == "gemini-2.5-flash"
    assert llm.get_model("gemini_pro") == "gemini-2.5-pro"
    assert llm.get_model("claude_sonnet").startswith("claude-")


def test_get_model_env_override(monkeypatch):
    monkeypatch.setenv("NK_MODEL_GEMINI_FLASH", "gemini-9-flash")
    assert llm.get_model("gemini_flash") == "gemini-9-flash"
    # otros tiers no afectados
    assert llm.get_model("gemini_pro") == "gemini-2.5-pro"


def test_get_model_unknown_tier():
    with pytest.raises(KeyError):
        llm.get_model("gpt5")


def test_all_models_applies_overrides(monkeypatch):
    monkeypatch.setenv("NK_MODEL_IMAGEN", "imagen-5.0")
    models = llm.all_models()
    assert models["imagen"] == "imagen-5.0"
    assert set(models) == {"gemini_flash", "gemini_pro", "imagen", "claude_sonnet"}


def test_provider_of():
    assert llm.provider_of("gemini_flash") == "gemini"
    assert llm.provider_of("claude_sonnet") == "anthropic"
    with pytest.raises(KeyError):
        llm.provider_of("nope")


def test_no_retired_models_in_defaults():
    """Guard L-014: modelos retirados no pueden volver al registro."""
    retired = {"gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.0-pro"}
    assert not retired & set(llm.all_models().values())


def test_gemini_config_sdk_default_thinking_zero():
    cfg = llm.gemini_config_sdk(max_output_tokens=1024, temperature=0.3)
    assert cfg["thinking_config"] == {"thinking_budget": 0}
    assert cfg["max_output_tokens"] == 1024
    assert cfg["temperature"] == 0.3


def test_gemini_config_sdk_dynamic_thinking():
    cfg = llm.gemini_config_sdk(thinking_budget=None, max_output_tokens=4096)
    assert "thinking_config" not in cfg


def test_gemini_config_rest_camelcase():
    cfg = llm.gemini_config_rest(maxOutputTokens=2048)
    assert cfg["thinkingConfig"] == {"thinkingBudget": 0}
    assert cfg["maxOutputTokens"] == 2048
    assert "thinking_config" not in cfg
