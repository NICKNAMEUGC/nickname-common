"""Transporte del plano IA, 2026-10-18 (D-F + entregables 2-8 de la ola 1).

Todo offline: http_post inyectado, cero red. Cubre:
- D-F: Gemini 3.x y su razonamiento mínimo por modelo (gemini_config_* y leg directo).
- complete_for(tier=...): misma secuencia de vías de la clase con el tier fijado.
- Semántica de router en complete_for (clave basta; NK_ROUTER=off es el rollback).
- Leg OpenRouter: provider.data_collection="deny" por defecto y reasoning.
- finish_reason normalizado en todos los legs.
- Mensajes multimodales por leg, stop sequences y strict configurable.
"""

import json
import logging

import pytest

from nickname_common import ai_policy, llm
from nickname_common.budget import BudgetGuard

_MSG = [{"role": "user", "content": "hola"}]
_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
           "required": ["ok"], "additionalProperties": False}
_EXENTO = "test-offline"
_PNG = "data:image/png;base64,iVBORw0KGgo="
_PDF_B64 = "JVBERi0xLjQK"
_MULTI = [
    {"role": "system", "content": [{"type": "text", "text": "eres breve"}]},
    {"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": _PNG}},
        {"type": "image_url", "image_url": {"url": "https://example.com/foto.png"}},
        {"type": "file", "file": {"filename": "f.pdf",
                                  "file_data": f"data:application/pdf;base64,{_PDF_B64}"}},
    ]},
]


@pytest.fixture(autouse=True)
def _entorno_limpio(monkeypatch):
    for var in (llm.ROUTER_ENV, llm.ROUTER_KEY_ENV, llm.ROUTER_AGENT_ENV,
                llm.XAI_KEY_ENV, llm.XAI_LIVE_ENV,
                llm.GEMINI_KEY_ENV, llm.GEMINI_KEY_FALLBACK_ENV,
                llm.OPENAI_KEY_ENV, llm.ANTHROPIC_KEY_ENV,
                llm.OPENAI_FALLBACK_MODEL_ENV, llm.ANTHROPIC_FALLBACK_MODEL_ENV):
        monkeypatch.delenv(var, raising=False)
    for tier in llm.all_tiers():
        monkeypatch.delenv(llm.ENV_PREFIX + tier.upper(), raising=False)
        monkeypatch.delenv(llm.ENV_PREFIX + tier.upper() + "_SLUG", raising=False)


class Red:
    """http_post falso: devuelve siempre `respuesta` según el host."""

    def __init__(self, **por_host):
        self.por_host = por_host
        self.llamadas = []

    def __call__(self, url, headers, body, timeout):
        self.llamadas.append({"url": url, "body": body})
        for host, (status, payload) in self.por_host.items():
            if host in url:
                return status, json.dumps(payload)
        raise AssertionError(f"llamada inesperada: {url}")

    @property
    def body(self):
        return self.llamadas[-1]["body"]


def _chat(finish="stop", contenido='{"ok": true}'):
    return {"choices": [{"message": {"content": contenido}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
                      "cost": 0.0001}}


def _gemini(finish="STOP", parts=None):
    return {"candidates": [{"content": {"parts": parts or [{"text": '{"ok": true}'}]},
                            "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2,
                              "totalTokenCount": 5}}


def _anthropic(stop_reason="end_turn"):
    return {"content": [{"type": "text", "text": '{"ok": true}'}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 3, "output_tokens": 2}}


# --- D-F: registro Gemini 3.x -------------------------------------------------

class TestRegistroDF:
    def test_defaults_gemini_3(self):
        assert llm.get_model("gemini_flash") == "gemini-3.8-flash"
        assert llm.get_model("gemini_flash_lite") == "gemini-3.5-flash-lite"
        assert llm.get_model("gemini_pro") == "gemini-4-argon"

    def test_slugs_openrouter_gemini_3(self):
        assert llm.router_slug("gemini_flash") == "google/gemini-3.8-flash"
        assert llm.router_slug("gemini_flash_lite") == "google/gemini-3.5-flash-lite"
        assert llm.router_slug("gemini_pro") == "google/gemini-4-argon"

    def test_ningun_tier_apunta_a_gemini_2_5(self):
        retirados_20261020 = {"gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"}
        assert not retirados_20261020 & set(llm.all_models().values())
        assert not {f"google/{m}" for m in retirados_20261020} & set(llm._ROUTER_SLUGS.values())  # noqa: SLF001

    def test_los_tiers_gemini_tienen_precio_en_el_catalogo(self):
        from nickname_common.cost_catalog import known_models
        for tier in ("gemini_flash", "gemini_flash_lite", "gemini_pro", "claude_sonnet",
                     "claude_opus", "claude_haiku", "gpt_quality", "gpt_fast"):
            assert llm.get_model(tier) in known_models(), tier


class TestThinkingGemini3:
    @pytest.mark.parametrize("tier, esperado", [
        ("gemini_flash_lite", {"thinkingLevel": "minimal"}),
        ("gemini_flash", {"thinkingBudget": 0}),
        # gemini-4-argon acepta thinkingBudget=0 y rechaza minimal (API, 2026-10-18).
        ("gemini_pro", {"thinkingBudget": 0}),
    ])
    def test_rest_por_tier(self, tier, esperado):
        cfg = llm.gemini_config_rest(tier=tier, maxOutputTokens=64)
        assert cfg.get("thinkingConfig") == esperado
        assert cfg["maxOutputTokens"] == 64

    def test_sdk_por_modelo_en_snake_case(self):
        cfg = llm.gemini_config_sdk(model="gemini-3.5-flash-lite", max_output_tokens=64)
        assert cfg["thinking_config"] == {"thinking_level": "minimal"}
        cfg = llm.gemini_config_sdk(model="gemini-3.8-flash")
        assert cfg["thinking_config"] == {"thinking_budget": 0}
        assert "thinking_config" not in llm.gemini_config_sdk(model="gemini-3.1-pro-preview")
        cfg = llm.gemini_config_sdk(model="gemini-4-argon")
        assert cfg["thinking_config"] == {"thinking_budget": 0}

    def test_sin_modelo_conserva_el_comportamiento_legado(self):
        assert llm.gemini_config_rest()["thinkingConfig"] == {"thinkingBudget": 0}
        assert llm.gemini_config_sdk()["thinking_config"] == {"thinking_budget": 0}

    def test_thinking_level_explicito(self):
        cfg = llm.gemini_config_rest(thinking_level="high", model="gemini-3.1-pro-preview")
        assert cfg["thinkingConfig"] == {"thinkingLevel": "high"}
        cfg = llm.gemini_config_sdk(thinking_level="low")
        assert cfg["thinking_config"] == {"thinking_level": "low"}

    def test_budget_y_level_son_excluyentes(self):
        with pytest.raises(ValueError):
            llm.gemini_config_rest(thinking_budget=512, thinking_level="low")

    def test_budget_positivo_se_respeta(self):
        cfg = llm.gemini_config_rest(thinking_budget=512, model="gemini-3.5-flash-lite")
        assert cfg["thinkingConfig"] == {"thinkingBudget": 512}

    @pytest.mark.parametrize("modelo, esperado", [
        ("gemini-9-flash", {"thinkingBudget": 0}),
        ("gemini-9-pro", None),
    ])
    def test_modelo_fuera_de_tabla_usa_la_regla_historica(self, modelo, esperado):
        assert llm.gemini_config_rest(model=modelo).get("thinkingConfig") == esperado

    @pytest.mark.parametrize("tier, reasoning, esperado", [
        ("gemini_flash_lite", None, {"thinkingLevel": "minimal"}),
        ("gemini_flash", None, {"thinkingBudget": 0}),
        ("gemini_pro", None, None),
        ("gemini_pro", {"effort": "low"}, {"thinkingLevel": "low"}),
        ("gemini_flash", {"effort": "xhigh"}, {"thinkingLevel": "high"}),
        ("gemini_flash", {"max_tokens": 256}, {"thinkingBudget": 256}),
        ("gemini_flash_lite", {"enabled": False}, {"thinkingLevel": "minimal"}),
        ("gemini_flash", {"enabled": True}, None),
    ])
    def test_leg_directo_traduce_reasoning(self, monkeypatch, tier, reasoning, esperado):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, _gemini())})
        llm._leg_gemini_direct(tier, _MSG, None, 64, None, red, 5.0,  # noqa: SLF001
                               reasoning=reasoning)
        assert red.body["generationConfig"].get("thinkingConfig") == esperado

    def test_partes_de_razonamiento_no_entran_en_el_texto(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        partes = [{"text": "pensando...", "thought": True}, {"text": "ok"}]
        red = Red(**{"generativelanguage": (200, _gemini(parts=partes))})
        res = llm._leg_gemini_direct("gemini_pro", _MSG, None, 64, None, red, 5.0)  # noqa: SLF001
        assert res.text == "ok"


# --- complete_for(tier=...) ---------------------------------------------------

class TestTierFijado:
    @pytest.mark.parametrize("clase, tier, esperado", [
        ("clasificacion_masiva", "gemini_pro",
         [("gemini_pro", "gemini_direct"), ("gemini_pro", "openrouter")]),
        ("clasificacion_masiva", "claude_haiku", [("claude_haiku", "openrouter")]),
        ("cliente_dinero_tiempo_real", "claude_opus",
         [("claude_opus", "openrouter"), ("claude_opus", "anthropic_direct")]),
        ("cliente_dinero_tiempo_real", "gpt_fast",
         [("gpt_fast", "openai_direct"), ("gpt_fast", "openrouter")]),
        # Sin cadena API: la cadena legado del tier (openrouter → directo).
        ("codigo", "claude_sonnet",
         [("claude_sonnet", "openrouter"), ("claude_sonnet", "anthropic_direct")]),
        ("codigo", "gemini_flash", [("gemini_flash", "openrouter"), ("gemini_flash", "gemini_direct")]),
        ("codigo", "grok_fast", [("grok_fast", "openrouter")]),
    ])
    def test_cadena_con_tier_fijado(self, clase, tier, esperado):
        pasos = ai_policy.api_chain_for_tier(clase, tier)
        assert [(p.tier, p.via) for p in pasos] == esperado

    def test_tier_fijado_recorre_las_vias_de_la_clase(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, _gemini())})
        res = llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red, tier="gemini_flash")
        assert "/gemini-3.8-flash:" in red.llamadas[0]["url"]
        assert len(red.llamadas) == 1
        assert res.provider == "google-direct"
        assert res.usage_event.reason == f"clase:clasificacion_masiva;tier:gemini_flash;budget_exempt:{_EXENTO}"

    def test_tier_fijado_conserva_budget_guard_y_evento(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")

        class Almacen:
            sumas = []

            def get_month_used_micros(self):
                return 0

            def add_spent_micros(self, amount):
                self.sumas.append(amount)

        almacen = Almacen()
        guard = BudgetGuard(store=almacen, month_limit_micros=10_000_000)
        red = Red(**{"openrouter.ai": (200, _chat())})
        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard,
                               http_post=red, tier="claude_haiku")
        assert red.body["model"] == "anthropic/claude-haiku-5.5"
        assert almacen.sumas == [100]
        assert res.usage_event.requested_model == "anthropic/claude-haiku-5.5"

    def test_tier_fijado_respeta_la_autorizacion_d_a(self, monkeypatch):
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=Red(), tier="claude_opus")
        assert "anthropic-direct[claude_opus](no-autorizado-D-A)" in str(exc.value)

    def test_tier_que_no_es_de_chat(self):
        with pytest.raises(llm.ClassNotApiExecutable):
            llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=Red(), tier="gemini_embedding")

    def test_tier_desconocido(self):
        with pytest.raises(KeyError):
            llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=Red(), tier="gemini_ultra")

    def test_override_roto_del_tier_fijado_revienta_antes_de_la_red(self, monkeypatch):
        monkeypatch.setenv("NK_MODEL_CLAUDE_OPUS_SLUG", "openai/gpt-6-astra")
        red = Red()
        with pytest.raises(llm.CrossProviderOverrideError):
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red, tier="claude_opus")
        assert red.llamadas == []


# --- Router en complete_for ---------------------------------------------------

class TestRouterComplete_for:
    def test_router_allowed_por_defecto(self, monkeypatch):
        assert llm.router_allowed_for_policy() is True
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        assert llm.router_allowed_for_policy() is True
        monkeypatch.setenv(llm.ROUTER_ENV, "off")
        assert llm.router_allowed_for_policy() is False

    def test_complete_legado_sigue_exigiendo_nk_router(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        assert llm.router_enabled() is False
        with pytest.raises(NotImplementedError):
            llm.complete("gemini_flash", _MSG, http_post=Red())

    def test_sin_clave_y_sin_nk_router_no_avisa(self, monkeypatch, caplog):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, _gemini())})
        with caplog.at_level(logging.WARNING, logger="nickname_common.llm"):
            llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)
        assert llm.ROUTER_KEY_ENV not in caplog.text


# --- Leg OpenRouter: data_collection y reasoning ------------------------------

class TestOpenRouterPreferencias:
    @pytest.fixture(autouse=True)
    def _clave(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")

    def _llamar(self, clase="cliente_dinero_tiempo_real", **kw):
        red = Red(**{"openrouter.ai": (200, _chat())})
        llm.complete_for(clase, _MSG, guard=None, budget_exempt=_EXENTO, http_post=red, **kw)
        return red.body

    def test_data_collection_deny_por_defecto(self):
        assert self._llamar()["provider"]["data_collection"] == "deny"

    def test_data_collection_desactivable(self):
        assert "data_collection" not in self._llamar(data_collection=None)["provider"]
        assert "provider" not in self._llamar(data_collection=None, byok_provider_order=False)
        assert self._llamar(data_collection="allow")["provider"]["data_collection"] == "allow"

    def test_data_collection_invalido(self):
        with pytest.raises(ValueError):
            self._llamar(data_collection="nunca")

    def test_complete_legado_solo_manda_el_orden_byok(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        red = Red(**{"openrouter.ai": (200, _chat())})
        llm.complete("claude_sonnet", _MSG, http_post=red)
        assert red.body["provider"] == {"order": ["anthropic"], "allow_fallbacks": True}
        assert "reasoning" not in red.body
        llm.complete("claude_sonnet", _MSG, http_post=red, byok_provider_order=False)
        assert "provider" not in red.body


    def test_reasoning_passthrough(self):
        assert self._llamar(reasoning={"enabled": False})["reasoning"] == {"enabled": False}
        assert self._llamar(reasoning={"effort": "high"})["reasoning"] == {"effort": "high"}

    def test_reasoning_por_defecto_solo_en_gemini_flash(self):
        assert "reasoning" not in self._llamar()
        assert self._llamar(tier="gemini_flash")["reasoning"] == {"effort": "low"}
        assert self._llamar(tier="gemini_flash_lite")["reasoning"] == {"effort": "low"}
        assert "reasoning" not in self._llamar(tier="gemini_pro")

    def test_reasoning_explicito_gana_al_por_defecto(self):
        assert self._llamar(tier="gemini_flash", reasoning={"effort": "medium"})["reasoning"] == {
            "effort": "medium"}

    def test_complete_legado_gemini_flash_protege_max_tokens(self, monkeypatch):
        """L-014 en OpenRouter: Gemini 3.x razona por defecto y se comería los
        16 tokens por defecto de complete()."""
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        red = Red(**{"openrouter.ai": (200, _chat())})
        llm.complete("gemini_flash_lite", _MSG, http_post=red)
        assert red.body["reasoning"] == {"effort": "low"}

    def test_reasoning_que_no_es_dict(self):
        with pytest.raises(TypeError):
            self._llamar(reasoning="low")


# --- finish_reason ------------------------------------------------------------

class TestFinishReason:
    @pytest.mark.parametrize("crudo, esperado", [
        ("stop", "stop"), ("length", "length"), ("content_filter", "content_filter"),
        ("tool_calls", "other"), ("error", "other"), (None, "other"),
    ])
    def test_openrouter(self, monkeypatch, crudo, esperado):
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        red = Red(**{"openrouter.ai": (200, _chat(finish=crudo))})
        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)
        assert res.finish_reason == esperado

    @pytest.mark.parametrize("crudo, esperado", [
        ("STOP", "stop"), ("MAX_TOKENS", "length"), ("SAFETY", "content_filter"),
        ("RECITATION", "content_filter"), ("PROHIBITED_CONTENT", "content_filter"),
        ("MALFORMED_FUNCTION_CALL", "other"), ("OTHER", "other"),
    ])
    def test_gemini_directo(self, monkeypatch, crudo, esperado):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, _gemini(finish=crudo))})
        res = llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)
        assert res.finish_reason == esperado

    def test_gemini_prompt_bloqueado(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, {"promptFeedback": {"blockReason": "SAFETY"}})})
        res = llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)
        assert res.finish_reason == "content_filter"

    @pytest.mark.parametrize("crudo, esperado", [
        ("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"), ("refusal", "content_filter"),
        ("tool_use", "other"), ("pause_turn", "other"),
    ])
    def test_anthropic_directo(self, monkeypatch, crudo, esperado):
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        monkeypatch.setenv(llm.ROUTER_AGENT_ENV, "nickname-recobro-agent")
        red = Red(**{"api.anthropic.com": (200, _anthropic(crudo))})
        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)
        assert res.finish_reason == esperado

    def test_openai_directo(self, monkeypatch):
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        red = Red(**{"api.openai.com": (200, _chat(finish="length"))})
        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)
        assert res.finish_reason == "length"

    def test_legados_tambien(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        red = Red(**{"openrouter.ai": (503, {}), "api.anthropic.com": (200, _anthropic("max_tokens"))})
        res = llm.complete("claude_sonnet", _MSG, http_post=red)
        assert res.provider == "anthropic-fallback"
        assert res.finish_reason == "length"

    def test_xai_legado(self, monkeypatch):
        red = Red(**{"api.x.ai": (200, _chat(finish="stop"))})
        res = llm.complete("grok_fast", _MSG, http_post=red)
        assert res.finish_reason == "stop"

    def test_default_del_dataclass(self):
        res = llm.LLMResult(text="", provider="x", model="m", http_status=200)
        assert res.finish_reason == "other"
        assert set(llm.FINISH_REASONS) == {"stop", "length", "content_filter", "other"}


# --- Multimodal ---------------------------------------------------------------

class TestMultimodal:
    def test_openrouter_tal_cual(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        red = Red(**{"openrouter.ai": (200, _chat())})
        llm.complete_for("cliente_dinero_tiempo_real", _MULTI, guard=None,
                         budget_exempt=_EXENTO, http_post=red)
        assert red.body["messages"] == _MULTI

    def test_openrouter_completa_file_data_sin_prefijo(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        red = Red(**{"openrouter.ai": (200, _chat())})
        msg = [{"role": "user", "content": [{"type": "file", "file": {"file_data": _PDF_B64}}]}]
        llm.complete_for("cliente_dinero_tiempo_real", msg, guard=None,
                         budget_exempt=_EXENTO, http_post=red)
        parte = red.body["messages"][0]["content"][0]
        assert parte == {"type": "file", "file": {
            "filename": "documento.pdf", "file_data": f"data:application/pdf;base64,{_PDF_B64}"}}

    def test_openai_directo_chat_completions(self, monkeypatch):
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        red = Red(**{"api.openai.com": (200, _chat())})
        llm.complete_for("cliente_dinero_tiempo_real", _MULTI, guard=None,
                         budget_exempt=_EXENTO, http_post=red)
        partes = red.body["messages"][1]["content"]
        assert [p["type"] for p in partes] == ["text", "image_url", "image_url", "file"]
        assert partes[1]["image_url"]["url"] == _PNG

    def test_anthropic_bloques_image_y_document(self, monkeypatch):
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        monkeypatch.setenv(llm.ROUTER_AGENT_ENV, "nickname-recobro-agent")
        red = Red(**{"api.anthropic.com": (200, _anthropic())})
        llm.complete_for("cliente_dinero_tiempo_real", _MULTI, guard=None,
                         budget_exempt=_EXENTO, http_post=red)
        body = red.body
        assert body["system"] == "eres breve"
        assert body["messages"][0]["content"] == [
            {"type": "text", "text": "describe"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "iVBORw0KGgo="}},
            {"type": "image", "source": {"type": "url", "url": "https://example.com/foto.png"}},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": _PDF_B64}},
        ]

    def test_anthropic_solo_pdf(self, monkeypatch):
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        monkeypatch.setenv(llm.ROUTER_AGENT_ENV, "nickname-recobro-agent")
        msg = [{"role": "user", "content": [{"type": "file", "file": {
            "file_data": "data:text/csv;base64,YQ=="}}]}]
        with pytest.raises(ValueError):
            llm.complete_for("cliente_dinero_tiempo_real", msg, guard=None,
                             budget_exempt=_EXENTO, http_post=Red())

    def test_gemini_inline_data_y_file_data(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, _gemini())})
        llm.complete_for("clasificacion_masiva", _MULTI, guard=None,
                         budget_exempt=_EXENTO, http_post=red)
        body = red.body
        assert body["systemInstruction"] == {"parts": [{"text": "eres breve"}]}
        assert body["contents"] == [{"role": "user", "parts": [
            {"text": "describe"},
            {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}},
            {"fileData": {"mimeType": "image/png", "fileUri": "https://example.com/foto.png"}},
            {"inlineData": {"mimeType": "application/pdf", "data": _PDF_B64}},
        ]}]

    def test_texto_plano_no_cambia(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, _gemini())})
        llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                         budget_exempt=_EXENTO, http_post=red)
        assert red.body["contents"] == [{"role": "user", "parts": [{"text": "hola"}]}]

    @pytest.mark.parametrize("contenido", [
        [{"type": "audio", "audio": {}}],
        [{"type": "image_url", "image_url": {"url": ""}}],
        [{"type": "image_url", "image_url": {"url": "data:image/png,sin-base64"}}],
        [{"type": "file", "file": {}}],
        ["texto suelto"],
    ])
    def test_partes_invalidas_son_error_de_programacion(self, monkeypatch, contenido):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, _gemini())})
        with pytest.raises(ValueError):
            llm.complete_for("clasificacion_masiva", [{"role": "user", "content": contenido}],
                             guard=None, budget_exempt=_EXENTO, http_post=red)
        assert red.llamadas == []

    def test_sistema_con_imagen_es_error(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        msg = [{"role": "system", "content": [{"type": "image_url", "image_url": {"url": _PNG}}]},
               {"role": "user", "content": "hola"}]
        with pytest.raises(ValueError):
            llm.complete_for("clasificacion_masiva", msg, guard=None,
                             budget_exempt=_EXENTO, http_post=Red())


# --- stop y strict ------------------------------------------------------------

class TestStopYStrict:
    def test_stop_en_openrouter_y_strict_configurable(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        red = Red(**{"openrouter.ai": (200, _chat())})
        llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None, budget_exempt=_EXENTO,
                         http_post=red, stop=["FIN"], json_schema=_SCHEMA,
                         json_schema_strict=False)
        assert red.body["stop"] == ["FIN"]
        assert red.body["response_format"]["json_schema"]["strict"] is False

    def test_strict_por_defecto_true(self, monkeypatch):
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        red = Red(**{"api.openai.com": (200, _chat())})
        llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None, budget_exempt=_EXENTO,
                         http_post=red, json_schema=_SCHEMA, stop=["FIN", "###"])
        assert red.body["response_format"]["json_schema"]["strict"] is True
        assert red.body["stop"] == ["FIN", "###"]

    def test_stop_en_anthropic(self, monkeypatch):
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        monkeypatch.setenv(llm.ROUTER_AGENT_ENV, "nickname-recobro-agent")
        red = Red(**{"api.anthropic.com": (200, _anthropic("stop_sequence"))})
        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red, stop=["FIN"])
        assert red.body["stop_sequences"] == ["FIN"]
        assert "stop" not in red.body
        assert res.finish_reason == "stop"

    def test_stop_en_gemini(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, _gemini())})
        llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                         budget_exempt=_EXENTO, http_post=red, stop=["FIN"])
        assert red.body["generationConfig"]["stopSequences"] == ["FIN"]

    def test_sin_stop_no_se_envia(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(**{"generativelanguage": (200, _gemini())})
        llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                         budget_exempt=_EXENTO, http_post=red, stop=[])
        assert "stopSequences" not in red.body["generationConfig"]

    @pytest.mark.parametrize("stop", ["FIN", [""], [1]])
    def test_stop_invalido(self, stop):
        with pytest.raises(ValueError):
            llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=Red(), stop=stop)


class TestOrdenProveedorByok:
    """provider.order hacia el endpoint con clave BYOK (2026-10-18)."""

    @pytest.fixture(autouse=True)
    def _clave(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")

    def _llamar(self, clase="cliente_dinero_tiempo_real", **kw):
        red = Red(**{"openrouter.ai": (200, _chat())})
        llm.complete_for(clase, _MSG, guard=None, budget_exempt=_EXENTO, http_post=red, **kw)
        return red.body

    @pytest.mark.parametrize("tier, orden", [
        ("gemini_pro", ["google-ai-studio"]),
        ("gemini_flash", ["google-ai-studio"]),
        ("gemini_flash_lite", ["google-ai-studio"]),
        ("claude_sonnet", ["anthropic"]),
        ("claude_opus", ["anthropic"]),
    ])
    def test_orden_por_familia_con_respaldo(self, tier, orden):
        prov = self._llamar(tier=tier)["provider"]
        assert prov == {"order": orden, "allow_fallbacks": True, "data_collection": "deny"}

    def test_gpt_no_lleva_orden(self):
        prov = self._llamar(tier="gpt_quality")["provider"]
        assert prov == {"data_collection": "deny"}

    def test_desactivable(self):
        prov = self._llamar(tier="gemini_pro", byok_provider_order=False)["provider"]
        assert prov == {"data_collection": "deny"}

    def test_complete_legado_gemini(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        red = Red(**{"openrouter.ai": (200, _chat())})
        llm.complete("gemini_pro", _MSG, http_post=red)
        assert red.body["provider"] == {"order": ["google-ai-studio"], "allow_fallbacks": True}


class TestCosteByokEnCompleteLegado:
    """P1: con BYOK, OpenRouter devuelve cost=0 (solo comisión) y el gasto real en
    cost_details.upstream_inference_cost (llamadas reales del 2026-10-18). Los
    consumidores de complete() suman usage["cost_micros"] a su BudgetGuard."""

    def _byok(self, cost=0, upstream=0.000244, is_byok=True):
        body = _chat()
        body["usage"] = {"prompt_tokens": 36, "completion_tokens": 5, "total_tokens": 41,
                         "cost": cost, "is_byok": is_byok,
                         "cost_details": {"upstream_inference_cost": upstream}}
        return body

    def _complete(self, monkeypatch, payload):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        red = Red(**{"openrouter.ai": (200, payload)})
        return llm.complete("gemini_pro", _MSG, http_post=red)

    def test_byok_suma_upstream(self, monkeypatch):
        res = self._complete(monkeypatch, self._byok())
        assert res.usage["cost_micros"] == 244
        assert "upstream_cost_micros" not in res.usage and "is_byok" not in res.usage

    def test_byok_con_comision(self, monkeypatch):
        res = self._complete(monkeypatch, self._byok(cost=0.00001))
        assert res.usage["cost_micros"] == 254

    def test_sin_byok_no_cuenta_dos_veces(self, monkeypatch):
        res = self._complete(monkeypatch, self._byok(cost=0.0001, upstream=0.0001,
                                                       is_byok=False))
        assert res.usage["cost_micros"] == 100

    def test_complete_for_no_cuenta_dos_veces(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        red = Red(**{"openrouter.ai": (200, self._byok())})
        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red, tier="gemini_pro")
        assert res.usage_event.cost_micros == 244
