"""Tests del registro central de modelos (F0.1) y adapter xAI Fase 1."""

import json

import pytest

from nickname_common import llm
from nickname_common.llm import (
    CrossProviderOverrideError,
    LLMResult,
    ProviderNotConfigured,
)


def test_get_model_defaults():
    assert llm.get_model("gemini_flash") == "gemini-2.5-flash"
    assert llm.get_model("gemini_pro") == "gemini-2.5-pro"
    assert llm.get_model("claude_sonnet").startswith("claude-")
    assert llm.get_model("grok_fast") == "grok-3-mini"
    assert llm.get_model("grok_quality") == "grok-4"


def test_get_model_env_override(monkeypatch):
    monkeypatch.setenv("NK_MODEL_GEMINI_FLASH", "gemini-9-flash")
    assert llm.get_model("gemini_flash") == "gemini-9-flash"
    assert llm.get_model("gemini_pro") == "gemini-2.5-pro"


def test_get_model_unknown_tier():
    with pytest.raises(KeyError):
        llm.get_model("gpt5")


def test_all_models_applies_overrides(monkeypatch):
    monkeypatch.setenv("NK_MODEL_IMAGEN", "imagen-5.0")
    models = llm.all_models()
    assert models["imagen"] == "imagen-5.0"
    assert set(models) == {
        "gemini_flash",
        "gemini_pro",
        "imagen",
        "claude_sonnet",
        "grok_fast",
        "grok_quality",
    }


def test_provider_of():
    assert llm.provider_of("gemini_flash") == "gemini"
    assert llm.provider_of("imagen") == "gemini"
    assert llm.provider_of("claude_sonnet") == "anthropic"
    assert llm.provider_of("grok_fast") == "xai"
    assert llm.provider_of("grok_quality") == "xai"
    with pytest.raises(KeyError):
        llm.provider_of("nope")


def test_gemini_override_cannot_point_at_grok(monkeypatch):
    monkeypatch.setenv("NK_MODEL_GEMINI_FLASH", "grok-4")
    with pytest.raises(CrossProviderOverrideError, match="familia"):
        llm.get_model("gemini_flash")


def test_grok_override_cannot_point_at_claude(monkeypatch):
    monkeypatch.setenv("NK_MODEL_GROK_FAST", "claude-sonnet-4-20250514")
    with pytest.raises(CrossProviderOverrideError):
        llm.get_model("grok_fast")


def test_no_retired_models_in_defaults():
    """Guard L-014: modelos retirados no pueden volver al registro."""
    retired = {"gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.0-pro"}
    assert not retired & set(llm.all_models().values())


def test_capabilities_grok_has_no_embeddings_or_images():
    grok = llm.capabilities("grok_quality")
    assert grok["text"] is True
    assert grok["json_schema"] is True
    assert grok["embeddings"] is False
    assert grok["images"] is False
    gemini = llm.capabilities("gemini_flash")
    assert gemini["embeddings"] is True
    imagen = llm.capabilities("imagen")
    assert imagen["images"] is True
    assert imagen["text"] is False


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


def test_complete_rejects_gemini_tiers():
    with pytest.raises(NotImplementedError, match="xAI"):
        llm.complete("gemini_flash", [{"role": "user", "content": "hi"}])


def test_complete_xai_without_key_fail_closed(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("NK_XAI_LIVE", raising=False)
    with pytest.raises(ProviderNotConfigured, match="XAI_API_KEY"):
        llm.complete("grok_fast", [{"role": "user", "content": "ping"}])


def test_complete_xai_with_key_but_live_gated(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test-not-live")
    monkeypatch.delenv("NK_XAI_LIVE", raising=False)
    with pytest.raises(ProviderNotConfigured, match="NK_XAI_LIVE"):
        llm.complete("grok_fast", [{"role": "user", "content": "ping"}])


def test_complete_xai_injected_transport_never_hits_network(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("NK_XAI_LIVE", raising=False)
    captured = {}

    def fake_post(url, headers, body, timeout):
        captured["url"] = url
        captured["body"] = body
        captured["timeout"] = timeout
        payload = {
            "choices": [
                {"message": {"role": "assistant", "content": json.dumps({"ok": True})}}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        return 200, json.dumps(payload)

    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    result = llm.complete(
        "grok_quality",
        [{"role": "user", "content": "ping"}],
        json_schema=schema,
        http_post=fake_post,
        max_tokens=8,
    )
    assert captured["url"] == llm.XAI_CHAT_URL
    assert captured["body"]["model"] == "grok-4"
    assert captured["body"]["max_tokens"] == 8
    assert captured["body"]["response_format"]["type"] == "json_schema"
    assert isinstance(result, LLMResult)
    assert result.provider == "xai"
    assert result.model == "grok-4"
    assert result.http_status == 200
    assert result.parsed_json == {"ok": True}
    assert result.usage["total_tokens"] == 5
    assert captured["timeout"] == 30.0
