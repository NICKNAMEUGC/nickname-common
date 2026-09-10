"""Tests del tier gemini_flash_lite (WI-1, SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911).

Cubre exactamente lo que el tier promete: nombre de modelo, slug de
OpenRouter, familia protegida por el guard L-014, capacidades y el manejo de
thinking_budget en el leg directo — mismo patrón que los tests existentes de
gemini_flash/gemini_pro en test_llm.py y test_llm_cascade.py.
"""

import json

import pytest

from nickname_common import llm
from nickname_common.llm import CrossProviderOverrideError


def test_get_model_default():
    assert llm.get_model("gemini_flash_lite") == "gemini-2.5-flash-lite"


def test_get_model_env_override_same_family(monkeypatch):
    monkeypatch.setenv("NK_MODEL_GEMINI_FLASH_LITE", "gemini-9-flash-lite")
    assert llm.get_model("gemini_flash_lite") == "gemini-9-flash-lite"
    # Los demás tiers gemini no se ven afectados por el override de éste.
    assert llm.get_model("gemini_flash") == "gemini-2.5-flash"


def test_env_override_cannot_cross_family(monkeypatch):
    monkeypatch.setenv("NK_MODEL_GEMINI_FLASH_LITE", "grok-4")
    with pytest.raises(CrossProviderOverrideError, match="familia"):
        llm.get_model("gemini_flash_lite")


def test_provider_of_is_gemini():
    assert llm.provider_of("gemini_flash_lite") == "gemini"


def test_router_slug_follows_google_pattern():
    assert llm.router_slug("gemini_flash_lite") == "google/gemini-2.5-flash-lite"


def test_router_slug_env_override(monkeypatch):
    monkeypatch.setenv("NK_MODEL_GEMINI_FLASH_LITE_SLUG", "google/gemini-3-flash-lite")
    assert llm.router_slug("gemini_flash_lite") == "google/gemini-3-flash-lite"


def test_capabilities_mirror_gemini_flash():
    flash = llm.capabilities("gemini_flash")
    lite = llm.capabilities("gemini_flash_lite")
    assert lite == flash, (
        "gemini_flash_lite es la misma familia/capacidad que gemini_flash — "
        "solo cambia coste/latencia, no qué puede hacer el tier"
    )


def test_all_models_includes_new_tier():
    assert "gemini_flash_lite" in llm.all_models()


def test_gemini_flash_lite_is_gemini_direct_eligible():
    """El tier debe participar en el leg de fallback directo (mismo trato
    que gemini_flash) — si no, un job que lo use se queda sin fallback de
    familia cuando OpenRouter falla, justo lo que SPEC §4.2 pide evitar."""
    assert "gemini_flash_lite" in llm._GEMINI_DIRECT_TIERS  # noqa: SLF001 — test de invariante interno


class TestDirectLegThinkingBudget:
    """El leg google-direct debe mandar thinking_budget=0 para flash-lite,
    igual que para gemini_flash (solo gemini_pro es la excepción L-014)."""

    def _capturar_post(self):
        capturado = {}

        def _post(url, headers, body, timeout):
            capturado["url"] = url
            capturado["body"] = body
            return 200, json.dumps({
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 7,
                },
            })

        return _post, capturado

    def test_thinking_budget_zero_by_default(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "test-key")
        post, capturado = self._capturar_post()
        resultado = llm._leg_gemini_direct(  # noqa: SLF001 — mismo patrón que tests existentes de este leg
            "gemini_flash_lite",
            [{"role": "user", "content": "hola"}],
            json_schema=None,
            max_tokens=16,
            temperature=0.0,
            post=post,
            timeout=5.0,
        )
        assert resultado.provider == "google-direct"
        assert resultado.model == "gemini-2.5-flash-lite"
        gen_cfg = capturado["body"]["generationConfig"]
        assert gen_cfg["thinkingConfig"] == {"thinkingBudget": 0}
