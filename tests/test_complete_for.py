"""complete_for(clase, ...) — la puerta nueva del plano IA (ola 1, 2026-10-17).

Todo offline: http_post inyectado, cero red. Cubre la resolución de cada
clase, los saltos por falta de clave o de NK_ROUTER, el encadenado solo por
fallos de transporte, el presupuesto obligatorio, el LLMUsageEventV1 adjunto
y que complete() legado no cambia.
"""

import json
import logging

import pytest

from nickname_common import llm
from nickname_common.budget import BudgetBlockedReason, BudgetExhausted, BudgetGuard
from nickname_common.cost_catalog import estimate_cost_micros
from nickname_common.models import CostSource, LLMOutcome, LLMUsageEventV1

_SECRETO = "texto-privado-del-cliente"
_MSG = [{"role": "system", "content": "eres breve"},
        {"role": "user", "content": _SECRETO}]
_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
_EXENTO = "test-offline"


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


def _router(monkeypatch, con_key=True):
    monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
    if con_key:
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")


def _todas_las_claves(monkeypatch):
    _router(monkeypatch)
    monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
    monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
    monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")


def _agente_critico(monkeypatch, nombre="nickname-recobro-agent"):
    """D-A: identidad de despliegue autorizada a la clave Anthropic directa."""
    monkeypatch.setenv(llm.ROUTER_AGENT_ENV, nombre)


class Red:
    """http_post falso. Reglas (substring de URL, modelo|None) → respuesta.

    La primera regla que casa gana; `modelo` se compara con body["model"] (o
    con la URL en Gemini directo, donde el modelo va en la ruta).
    """

    def __init__(self, *reglas):
        self.reglas = reglas
        self.llamadas = []

    def __call__(self, url, headers, body, timeout):
        self.llamadas.append({"url": url, "headers": dict(headers), "body": body,
                              "timeout": timeout})
        for url_sub, modelo, resultado in self.reglas:
            if url_sub not in url:
                continue
            if modelo is not None and modelo != body.get("model") and f"/{modelo}:" not in url:
                continue
            if isinstance(resultado, Exception):
                raise resultado
            status, payload = resultado
            return status, payload if isinstance(payload, str) else json.dumps(payload)
        raise AssertionError(f"llamada inesperada: {url} {body.get('model')}")

    def modelos(self):
        return [ll["body"].get("model") or ll["url"].split("/models/")[1].split(":")[0]
                for ll in self.llamadas]


def _chat(contenido='{"ok": true}', usage=None):
    return {"choices": [{"message": {"content": contenido}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5,
                               "total_tokens": 15}}


def _chat_con_coste(contenido='{"ok": true}', coste=0.0123, **extra):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
             "cost": coste}
    usage.update(extra)
    return _chat(contenido, usage)


def _gemini(contenido='{"ok": true}', **meta):
    usage = {"promptTokenCount": 700, "candidatesTokenCount": 300,
             "totalTokenCount": 1000}
    usage.update(meta)
    return {"candidates": [{"content": {"parts": [{"text": contenido}]}}],
            "usageMetadata": usage}


def _anthropic(contenido='{"ok": true}'):
    return {"content": [{"type": "thinking", "thinking": ""},
                        {"type": "text", "text": contenido}],
            "usage": {"input_tokens": 4, "output_tokens": 2}}


class Almacen:
    """BudgetStore en memoria."""

    def __init__(self, usado=0, falla=False):
        self.usado = usado
        self.falla = falla
        self.sumas = []
        self.lecturas = 0

    def get_month_used_micros(self):
        self.lecturas += 1
        if self.falla:
            raise OSError("volumen sin montar")
        return self.usado

    def add_spent_micros(self, amount_micros):
        self.sumas.append(amount_micros)
        self.usado += amount_micros


def _guard(usado=0, limite=5_000_000, por_llamada=None, falla=False):
    almacen = Almacen(usado=usado, falla=falla)
    return BudgetGuard(store=almacen, month_limit_micros=limite,
                       per_call_limit_micros=por_llamada), almacen


# --- Resolución por clase -----------------------------------------------------

class TestResolucionPorClase:
    def test_clasificacion_masiva_sale_por_flash_lite_directo(self, monkeypatch):
        """D2 + SPEC §4: Gemini directo primero; OpenRouter es el último recurso."""
        _todas_las_claves(monkeypatch)
        red = Red(("generativelanguage.googleapis.com", None, (200, _gemini())))
        guard, almacen = _guard()

        res = llm.complete_for("clasificacion_masiva", _MSG, guard=guard,
                               json_schema=_SCHEMA, http_post=red)

        assert red.modelos() == ["gemini-3.5-flash-lite"]
        assert res.provider == "google-direct"
        assert res.parsed_json == {"ok": True}
        assert almacen.sumas == [estimate_cost_micros(
            "gemini-3.5-flash-lite", input_tokens=700, output_tokens=300)]

    def test_clasificacion_masiva_respaldo_flash_antes_que_openrouter(self, monkeypatch):
        _todas_las_claves(monkeypatch)
        red = Red(
            ("generativelanguage.googleapis.com", "gemini-3.5-flash-lite", (503, {})),
            ("generativelanguage.googleapis.com", "gemini-3.8-flash", (200, _gemini())),
        )

        res = llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert red.modelos() == ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
        assert res.provider == "google-direct"

    def test_clasificacion_masiva_sin_gemini_directo_cae_a_openrouter(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste())))
        guard, almacen = _guard()

        res = llm.complete_for("clasificacion_masiva", _MSG, guard=guard,
                               json_schema=_SCHEMA, http_post=red)

        assert red.modelos() == ["google/gemini-3.5-flash-lite"]
        assert res.provider == "openrouter:google"
        assert almacen.sumas == [12300]

    def test_cliente_dinero_tiempo_real_sale_por_claude_sonnet_en_openrouter(self, monkeypatch):
        _router(monkeypatch)
        monkeypatch.setenv(llm.ROUTER_AGENT_ENV, "nickname-recobro-agent")
        red = Red(("openrouter.ai", None, (200, _chat_con_coste())))

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, json_schema=_SCHEMA, http_post=red)

        llamada = red.llamadas[0]
        assert llamada["body"]["model"] == "anthropic/claude-sonnet-5.5"
        assert llamada["headers"]["X-Title"] == "nickname-recobro-agent"
        assert llamada["body"]["response_format"]["type"] == "json_schema"
        assert "temperature" not in llamada["body"], "Claude 5.x rechaza temperature"
        assert llamada["body"]["max_tokens"] == 1024
        assert res.provider == "openrouter:anthropic"

    def test_cliente_dinero_asincrono_usa_la_misma_cadena_api_de_respaldo(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste())))

        res = llm.complete_for("cliente_dinero_asincrono", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert red.modelos() == ["anthropic/claude-sonnet-5.5"]
        assert res.provider == "openrouter:anthropic"

    def test_contexto_largo_gemini_pro_directo_sin_thinking_cero(self, monkeypatch):
        _todas_las_claves(monkeypatch)
        red = Red(("generativelanguage.googleapis.com", None, (200, _gemini("hola"))))

        res = llm.complete_for("contexto_largo", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert red.modelos() == ["gemini-3.1-pro-preview"]
        cfg = red.llamadas[0]["body"]["generationConfig"]
        assert "thinkingConfig" not in cfg, "gemini-3.1-pro-preview rechaza thinking_budget=0"
        assert "temperature" not in cfg
        assert res.provider == "google-direct"
        assert res.model == "gemini-3.1-pro-preview"

    def test_contexto_largo_sigue_por_claude_byok(self, monkeypatch):
        """SPEC §4: Gemini → Claude (BYOK, crédito Team) → OpenRouter."""
        _todas_las_claves(monkeypatch)
        red = Red(
            ("generativelanguage.googleapis.com", None, (500, {})),
            ("openrouter.ai", "anthropic/claude-sonnet-5.5", (200, _chat_con_coste("ok"))),
        )

        res = llm.complete_for("contexto_largo", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert red.modelos() == ["gemini-3.1-pro-preview", "anthropic/claude-sonnet-5.5"]
        assert res.provider == "openrouter:anthropic"

    def test_contexto_largo_acaba_en_gemini_pro_por_openrouter(self, monkeypatch):
        _router(monkeypatch)
        red = Red(
            ("openrouter.ai", "anthropic/claude-sonnet-5.5", (503, {})),
            ("openrouter.ai", "google/gemini-3.1-pro-preview", (200, _chat_con_coste("ok"))),
        )

        res = llm.complete_for("contexto_largo", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert red.modelos() == ["anthropic/claude-sonnet-5.5", "google/gemini-3.1-pro-preview"]
        assert res.provider == "openrouter:google"

    @pytest.mark.parametrize("clase", ["codigo", "revision", "coordinacion"])
    def test_clase_de_suscripcion_sin_cadena_api_falla_claro(self, monkeypatch, clase):
        _todas_las_claves(monkeypatch)
        red = Red()
        guard, almacen = _guard()

        with pytest.raises(llm.ClassNotApiExecutable, match="broker de suscripciones"):
            llm.complete_for(clase, _MSG, guard=guard, http_post=red)

        assert red.llamadas == []
        assert almacen.lecturas == 0, "ni siquiera se consulta el presupuesto"

    @pytest.mark.parametrize("clase", ["embeddings", "imagen"])
    def test_embeddings_e_imagen_no_pasan_por_el_chat(self, monkeypatch, clase):
        _todas_las_claves(monkeypatch)
        red = Red()
        with pytest.raises(llm.ClassNotApiExecutable, match="endpoint propio"):
            llm.complete_for(clase, _MSG, guard=None, budget_exempt=_EXENTO, http_post=red)
        assert red.llamadas == []

    def test_clase_desconocida(self):
        with pytest.raises(KeyError):
            llm.complete_for("nope", _MSG, guard=None, budget_exempt=_EXENTO, http_post=Red())


# --- Saltos por credencial y por NK_ROUTER ------------------------------------

class TestSaltos:
    def test_con_clave_y_sin_nk_router_sale_por_openrouter(self, monkeypatch):
        """2026-10-18: en complete_for basta la clave; la política decide."""
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        red = Red(("openrouter.ai", None, (200, _chat_con_coste())))

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert red.modelos() == ["anthropic/claude-sonnet-5.5"]
        assert res.provider == "openrouter:anthropic"

    @pytest.mark.parametrize("valor", ["off", "OFF", " off "])
    def test_nk_router_off_salta_los_pasos_openrouter(self, monkeypatch, valor):
        """Palanca de rollback: NK_ROUTER=off saca al servicio de OpenRouter
        aunque tenga la clave."""
        monkeypatch.setenv(llm.ROUTER_ENV, valor)
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        red = Red(("api.openai.com", None, (200, _chat())))

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert len(red.llamadas) == 1
        assert all("openrouter.ai" not in ll["url"] for ll in red.llamadas)
        assert res.provider == "openai-direct"

    def test_nk_router_off_gemini_caido_no_sale_por_openrouter(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "off")
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(("generativelanguage.googleapis.com", None, (503, {})))

        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)

        assert all("openrouter.ai" not in ll["url"] for ll in red.llamadas)
        assert "openrouter:google[gemini_flash_lite](router-apagado)" in str(exc.value)

    def test_router_encendido_sin_clave_salta_y_avisa_sin_contenido(self, monkeypatch, caplog):
        _router(monkeypatch, con_key=False)
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        red = Red(("api.openai.com", None, (200, _chat())))

        with caplog.at_level(logging.WARNING, logger="nickname_common.llm"):
            res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                                   budget_exempt=_EXENTO, http_post=red)

        assert res.provider == "openai-direct"
        assert llm.ROUTER_KEY_ENV in caplog.text
        assert _SECRETO not in caplog.text

    def test_anthropic_directo_solo_con_clave(self, monkeypatch):
        """D-A: sin ANTHROPIC_API_KEY (agentes no críticos) el respaldo directo
        de Claude se salta y la cadena sigue por GPT."""
        _router(monkeypatch)
        red = Red(
            ("openrouter.ai", "anthropic/claude-sonnet-5.5", (500, {})),
            ("openrouter.ai", "openai/gpt-6-astra", (200, _chat_con_coste())),
        )

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert red.modelos() == ["anthropic/claude-sonnet-5.5", "openai/gpt-6-astra"]
        assert all("api.anthropic.com" not in ll["url"] for ll in red.llamadas)
        assert res.provider == "openrouter:openai"

    @pytest.mark.parametrize("agente", [None, "nickname-email-marketing-agent",
                                        "nickname-fiscal-agent"])
    def test_anthropic_directo_con_clave_pero_sin_autorizacion_d_a_se_salta(
        self, monkeypatch, agente
    ):
        """D-A: la clave Anthropic compartida existe en servicios no críticos;
        tenerla no autoriza el respaldo directo."""
        _router(monkeypatch)
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        if agente:
            monkeypatch.setenv(llm.ROUTER_AGENT_ENV, agente)
        red = Red(
            ("openrouter.ai", "anthropic/claude-sonnet-5.5", (503, {})),
            ("openrouter.ai", "openai/gpt-6-astra", (200, _chat_con_coste())),
        )

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert all("api.anthropic.com" not in ll["url"] for ll in red.llamadas)
        assert res.provider == "openrouter:openai"

    def test_anthropic_directo_odoo_agent_solo_en_jobs_cfo(self, monkeypatch):
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        _agente_critico(monkeypatch, "nickname-odoo-agent")
        red = Red(("api.anthropic.com", None, (200, _anthropic())))

        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red, job_id="sync_deals")
        assert "anthropic-direct[claude_sonnet](no-autorizado-D-A)" in str(exc.value)
        assert red.llamadas == []

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red,
                               job_id="cfo_consolidado")
        assert res.provider == "anthropic-direct"

    def test_autorizacion_d_a_por_despliegue_no_por_kwarg_service(self, monkeypatch):
        """El kwarg `service` etiqueta el evento; no autoriza la clave directa."""
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        monkeypatch.setenv(llm.ROUTER_AGENT_ENV, "nickname-linkedin-agent")
        red = Red()

        with pytest.raises(llm.CascadeExhausted, match="no-autorizado-D-A"):
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red,
                             service="nickname-recobro-agent")
        assert red.llamadas == []

    def test_anthropic_directo_con_clave_responde_con_schema_nativo(self, monkeypatch):
        _router(monkeypatch)
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        _agente_critico(monkeypatch)
        red = Red(
            ("openrouter.ai", None, (502, {})),
            ("api.anthropic.com", None, (200, _anthropic())),
        )

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, json_schema=_SCHEMA,
                               max_tokens=512, http_post=red)

        llamada = red.llamadas[1]
        assert llamada["url"] == "https://api.anthropic.com/v1/messages"
        assert llamada["headers"]["x-api-key"] == "ak-test"
        cuerpo = llamada["body"]
        assert cuerpo["model"] == "claude-sonnet-5-5"
        assert cuerpo["max_tokens"] == 512
        assert "temperature" not in cuerpo
        assert cuerpo["output_config"] == {
            "format": {"type": "json_schema", "schema": _SCHEMA}
        }
        assert cuerpo["system"] == "eres breve", "sin instrucción de schema: va nativo"
        assert all(m["role"] != "system" for m in cuerpo["messages"])
        assert res.provider == "anthropic-direct"
        assert res.model == "claude-sonnet-5-5"
        assert res.parsed_json == {"ok": True}

    def test_openai_directo_usa_max_completion_tokens(self, monkeypatch):
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        red = Red(("api.openai.com", None, (200, _chat())))

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, json_schema=_SCHEMA,
                               max_tokens=300, http_post=red)

        cuerpo = red.llamadas[0]["body"]
        assert cuerpo["model"] == "gpt-6-astra"
        assert cuerpo["max_completion_tokens"] == 300
        assert "max_tokens" not in cuerpo, "los GPT de razonamiento lo rechazan"
        assert "temperature" not in cuerpo
        assert cuerpo["response_format"]["type"] == "json_schema"
        assert red.llamadas[0]["headers"]["Authorization"] == "Bearer oa-test"
        assert res.provider == "openai-direct"
        assert res.model == "gpt-6-astra"

    def test_temperature_explicita_si_se_envia(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(("generativelanguage.googleapis.com", None, (200, _gemini())))

        llm.complete_for("clasificacion_masiva", _MSG, guard=None, budget_exempt=_EXENTO,
                         temperature=0.0, http_post=red)

        assert red.llamadas[0]["body"]["generationConfig"]["temperature"] == 0.0

    def test_override_de_modelo_de_la_misma_familia_llega_al_paso_directo(self, monkeypatch):
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        _agente_critico(monkeypatch, "nickname-facturas-agent")
        monkeypatch.setenv("NK_MODEL_CLAUDE_SONNET", "claude-sonnet-5")
        red = Red(("api.anthropic.com", None, (200, _anthropic())))

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert red.llamadas[0]["body"]["model"] == "claude-sonnet-5"
        assert res.model == "claude-sonnet-5"

    def test_override_que_cruza_de_familia_revienta_no_encadena(self, monkeypatch):
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        monkeypatch.setenv("NK_MODEL_CLAUDE_SONNET", "gpt-6-astra")
        red = Red(("api.openai.com", None, (200, _chat())))

        with pytest.raises(llm.CrossProviderOverrideError):
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)
        assert red.llamadas == [], "un override roto es config, no transporte"

    def test_override_roto_revienta_antes_del_presupuesto_y_de_la_red(self, monkeypatch):
        """Se resuelven los modelos de TODOS los pasos antes de empezar: el
        override roto de un paso tardío no espera a que fallen los anteriores."""
        _router(monkeypatch)
        monkeypatch.setenv("NK_MODEL_GPT_QUALITY", "claude-sonnet-5-5")
        red = Red(("openrouter.ai", None, (200, _chat_con_coste())))
        guard, almacen = _guard()

        with pytest.raises(llm.CrossProviderOverrideError):
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)
        assert red.llamadas == []
        assert almacen.lecturas == 0

    @pytest.mark.parametrize("tier, slug", [
        ("claude_sonnet", "google/gemini-3.5-flash-lite"),
        ("claude_sonnet", "anthropic/gemini-3.8-flash"),
        ("gpt_quality", "x-ai/grok-4.7"),
    ])
    def test_override_de_slug_que_cruza_de_familia_revienta(self, monkeypatch, tier, slug):
        """Guard L-014 también en NK_MODEL_<TIER>_SLUG: un slug de emergencia
        no convierte una clase de cliente/dinero en Flash-Lite (D2)."""
        _router(monkeypatch)
        monkeypatch.setenv(llm.ENV_PREFIX + tier.upper() + "_SLUG", slug)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste())))

        with pytest.raises(llm.CrossProviderOverrideError, match="_SLUG"):
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)
        assert red.llamadas == []

    def test_sin_ninguna_credencial_agota_sin_tocar_la_red(self):
        red = Red()
        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)
        mensaje = str(exc.value)
        assert "openrouter:anthropic[claude_sonnet](sin-credencial)" in mensaje
        assert "anthropic-direct[claude_sonnet](sin-credencial)" in mensaje
        assert "openrouter:openai[gpt_quality](sin-credencial)" in mensaje
        assert "openai-direct[gpt_quality](sin-credencial)" in mensaje
        assert red.llamadas == []
        assert isinstance(exc.value, llm.ProviderNotConfigured)


# --- Reglas de encadenado -----------------------------------------------------

class TestEncadenado:
    @pytest.mark.parametrize("fallo", [
        (400, {}), (401, {}), (402, {}), (404, {}), (429, {}), (500, {}), (503, {}),
        (529, {}), TimeoutError("lento"), ConnectionResetError("reset"),
    ])
    def test_fallos_de_transporte_encadenan(self, monkeypatch, fallo):
        _todas_las_claves(monkeypatch)
        _agente_critico(monkeypatch)
        red = Red(
            ("openrouter.ai", None, fallo),
            ("api.anthropic.com", None, (200, _anthropic())),
        )

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, json_schema=_SCHEMA, http_post=red)

        assert res.provider == "anthropic-direct"
        assert len(red.llamadas) == 2

    def test_200_con_json_invalido_no_encadena_y_es_content_error(self, monkeypatch):
        _todas_las_claves(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste("esto no es json"))))

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, json_schema=_SCHEMA, http_post=red)

        assert len(red.llamadas) == 1, "no se relitiga en otro proveedor"
        assert res.text == "esto no es json"
        assert res.parsed_json is None
        assert res.usage_event.outcome is LLMOutcome.CONTENT_ERROR
        assert res.usage_event.cost_micros == 12300, "se pagó igualmente"

    def test_json_cortado_por_max_tokens_es_content_error(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste('{"ok": tr'))))
        guard, almacen = _guard()

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard,
                               json_schema=_SCHEMA, http_post=red)

        assert res.parsed_json is None
        assert res.usage_event.outcome is LLMOutcome.CONTENT_ERROR
        assert almacen.sumas == [12300]

    def test_sin_schema_un_texto_libre_es_served(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste("texto libre"))))
        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)
        assert res.usage_event.outcome is LLMOutcome.SERVED

    def test_200_con_envelope_que_no_es_json_propaga(self, monkeypatch):
        _todas_las_claves(monkeypatch)
        red = Red(("openrouter.ai", None, (200, "<html>no</html>")))
        with pytest.raises(json.JSONDecodeError):
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)
        assert len(red.llamadas) == 1

    def test_todo_falla_resume_la_cadena_sin_contenido(self, monkeypatch, caplog):
        _todas_las_claves(monkeypatch)
        _agente_critico(monkeypatch)
        cuerpo = {"error": _SECRETO}
        red = Red(
            ("openrouter.ai", None, (429, cuerpo)),
            ("api.anthropic.com", None, (529, cuerpo)),
            ("api.openai.com", None, TimeoutError(_SECRETO)),
        )

        with caplog.at_level(logging.WARNING, logger="nickname_common.llm"):
            with pytest.raises(llm.CascadeExhausted) as exc:
                llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                                 budget_exempt=_EXENTO, http_post=red)

        mensaje = str(exc.value)
        assert ("openrouter:anthropic[claude_sonnet](HTTP 429) -> "
                "anthropic-direct[claude_sonnet](HTTP 529) -> "
                "openai-direct[gpt_quality](TimeoutError) -> "
                "openrouter:openai[gpt_quality](HTTP 429)") in mensaje
        assert _SECRETO not in mensaje
        assert _SECRETO not in caplog.text
        assert len(red.llamadas) == 4
        evento = exc.value.usage_event
        assert isinstance(evento, LLMUsageEventV1)
        assert evento.outcome is LLMOutcome.TRANSPORT_ERROR
        assert evento.cost_source is CostSource.UNAVAILABLE
        assert evento.cost_micros is None

    def test_todo_timeout_es_outcome_timeout(self, monkeypatch):
        import urllib.error
        _todas_las_claves(monkeypatch)
        _agente_critico(monkeypatch)
        red = Red(
            ("openrouter.ai", None, TimeoutError("lento")),
            ("api.anthropic.com", None, urllib.error.URLError(TimeoutError("connect"))),
            ("api.openai.com", None, TimeoutError("lento")),
        )

        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)

        assert exc.value.usage_event.outcome is LLMOutcome.TIMEOUT

    def test_sin_ningun_intento_de_red_es_transport_error(self):
        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=Red())
        assert exc.value.usage_event.outcome is LLMOutcome.TRANSPORT_ERROR


# --- Cortocircuito por puerta caída -------------------------------------------

class TestPuertaCaida:
    @pytest.mark.parametrize("fallo", [
        (401, {}), (402, {}), TimeoutError("colgado"), ConnectionRefusedError("x"),
    ])
    def test_openrouter_caido_no_se_reintenta_en_la_misma_cadena(self, monkeypatch, fallo):
        """Red, timeout, 401 o 402 son de la clave/host de OpenRouter: el
        segundo paso @openrouter fallaría igual (y en tiempo real son dos
        timeouts seguidos)."""
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, fallo))

        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)

        assert len(red.llamadas) == 1
        assert "openrouter:openai[gpt_quality](puerta-caida)" in str(exc.value)

    @pytest.mark.parametrize("fallo", [(429, {}), (500, {}), (503, {}), (404, {})])
    def test_fallo_atribuible_al_modelo_si_prueba_otro_slug(self, monkeypatch, fallo):
        _router(monkeypatch)
        red = Red(
            ("openrouter.ai", "anthropic/claude-sonnet-5.5", fallo),
            ("openrouter.ai", "openai/gpt-6-astra", (200, _chat_con_coste())),
        )

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert res.provider == "openrouter:openai"

    def test_gemini_directo_colgado_salta_al_ultimo_recurso(self, monkeypatch):
        _todas_las_claves(monkeypatch)
        red = Red(
            ("generativelanguage.googleapis.com", None, TimeoutError("colgado")),
            ("openrouter.ai", None, (200, _chat_con_coste())),
        )

        res = llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)

        assert red.modelos() == ["gemini-3.5-flash-lite", "google/gemini-3.5-flash-lite"]
        assert res.provider == "openrouter:google"

    def test_la_puerta_caida_es_por_llamada(self, monkeypatch):
        _router(monkeypatch)
        caida = Red(("openrouter.ai", None, (402, {})))
        with pytest.raises(llm.CascadeExhausted):
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=caida)

        sana = Red(("openrouter.ai", None, (200, _chat_con_coste())))
        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=sana)
        assert res.provider == "openrouter:anthropic"


# --- Presupuesto obligatorio --------------------------------------------------

class TestPresupuesto:
    def test_sin_guard_ni_exencion_falla_antes_de_la_red(self, monkeypatch):
        _todas_las_claves(monkeypatch)
        red = Red()
        with pytest.raises(ValueError, match="budget_exempt"):
            llm.complete_for("clasificacion_masiva", _MSG, guard=None, http_post=red)
        assert red.llamadas == []

    def test_guard_es_obligatorio_como_argumento(self):
        with pytest.raises(TypeError):
            llm.complete_for("clasificacion_masiva", _MSG, http_post=Red())  # noqa

    @pytest.mark.parametrize("motivo", ["", "   "])
    def test_exencion_vacia_no_vale(self, motivo):
        with pytest.raises(ValueError):
            llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                             budget_exempt=motivo, http_post=Red())

    def test_guard_que_no_es_budget_guard(self):
        with pytest.raises(TypeError, match="BudgetGuard"):
            llm.complete_for("clasificacion_masiva", _MSG, guard=object(), http_post=Red())

    def test_guard_y_exencion_son_excluyentes(self):
        guard, _ = _guard()
        with pytest.raises(ValueError, match="excluyentes"):
            llm.complete_for("clasificacion_masiva", _MSG, guard=guard,
                             budget_exempt="x", http_post=Red())

    def test_mes_agotado_no_llama_a_la_red(self, monkeypatch):
        _todas_las_claves(monkeypatch)
        red = Red()
        guard, almacen = _guard(usado=5_000_000, limite=5_000_000)

        with pytest.raises(BudgetExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        assert exc.value.reason is BudgetBlockedReason.MONTH_EXHAUSTED
        assert red.llamadas == []
        assert almacen.sumas == []
        evento = exc.value.usage_event
        assert evento.outcome is LLMOutcome.BUDGET_BLOCKED
        assert evento.provider == "none"
        assert evento.requested_model == "anthropic/claude-sonnet-5.5"
        assert evento.cost_micros is None

    def test_store_ilegible_bloquea_sin_red(self, monkeypatch):
        _todas_las_claves(monkeypatch)
        red = Red()
        guard, _ = _guard(falla=True)
        with pytest.raises(BudgetExhausted) as exc:
            llm.complete_for("clasificacion_masiva", _MSG, guard=guard, http_post=red)
        assert exc.value.reason is BudgetBlockedReason.STORE_UNAVAILABLE
        assert red.llamadas == []

    def test_registra_el_coste_del_proveedor(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(coste=0.0042))))
        guard, almacen = _guard()

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        assert almacen.sumas == [4200]
        assert res.usage_event.cost_micros == 4200
        assert res.usage_event.cost_source is CostSource.PROVIDER_RESPONSE

    def test_coste_de_catalogo_en_gemini_directo(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(("generativelanguage.googleapis.com", None, (200, _gemini())))
        guard, almacen = _guard()

        res = llm.complete_for("clasificacion_masiva", _MSG, guard=guard, http_post=red)

        esperado = estimate_cost_micros("gemini-3.5-flash-lite", input_tokens=700,
                                        output_tokens=300)
        assert esperado is not None and esperado > 0
        assert res.usage_event.cost_source is CostSource.CATALOG
        assert res.usage_event.cost_micros == esperado
        assert almacen.sumas == [esperado]

    def test_paso_directo_sin_precio_verificado_se_salta_con_guard(self, monkeypatch):
        """Fail-closed: Anthropic/OpenAI directos no informan coste; si el
        catálogo no tiene precio verificado del modelo (aquí, overrides de la
        misma familia sin precio), con guard el paso se salta en vez de gastar
        sin que el presupuesto lo vea."""
        monkeypatch.setenv("NK_MODEL_CLAUDE_SONNET", "claude-sonnet-9")
        monkeypatch.setenv("NK_MODEL_GPT_QUALITY", "gpt-9")
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        _agente_critico(monkeypatch)
        red = Red()
        guard, almacen = _guard(limite=1, por_llamada=1)

        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        mensaje = str(exc.value)
        assert "anthropic-direct[claude_sonnet](sin-precio)" in mensaje
        assert "openai-direct[gpt_quality](sin-precio)" in mensaje
        assert red.llamadas == []
        assert almacen.sumas == []

    def test_openrouter_402_no_abre_gasto_directo_sin_tope(self, monkeypatch):
        """Tope diario de la clave de OpenRouter agotado (402): la cadena no
        acaba en una vía directa que el guard no pueda contar."""
        monkeypatch.setenv("NK_MODEL_CLAUDE_SONNET", "claude-sonnet-9")
        monkeypatch.setenv("NK_MODEL_GPT_QUALITY", "gpt-9")
        _todas_las_claves(monkeypatch)
        _agente_critico(monkeypatch)
        red = Red(("openrouter.ai", None, (402, {})))
        guard, almacen = _guard()

        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        assert len(red.llamadas) == 1
        assert "(sin-precio)" in str(exc.value)
        assert "(puerta-caida)" in str(exc.value)
        assert almacen.sumas == []

    def test_openrouter_402_y_directo_con_precio_lo_cuenta_el_guard(self, monkeypatch):
        """Item 9 (2026-10-18): con precio verificado el respaldo directo de
        Claude se ejecuta con guard y suma su coste de catálogo, nunca 0."""
        _todas_las_claves(monkeypatch)
        _agente_critico(monkeypatch)
        red = Red(("openrouter.ai", None, (402, {})),
                  ("api.anthropic.com", None, (200, _anthropic())))
        guard, almacen = _guard()

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        esperado = estimate_cost_micros("claude-sonnet-5-5", input_tokens=4, output_tokens=2)
        assert esperado == 28
        assert res.provider == "anthropic-direct"
        assert res.usage_event.cost_source is CostSource.CATALOG
        assert res.usage_event.cost_micros == esperado
        assert almacen.sumas == [esperado]

    def test_openai_directo_con_precio_suma_catalogo(self, monkeypatch):
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        red = Red(("api.openai.com", None, (200, _chat())))
        guard, almacen = _guard()

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        assert res.provider == "openai-direct"
        esperado = estimate_cost_micros("gpt-6-astra", input_tokens=10, output_tokens=5)
        assert res.usage_event.cost_micros == esperado == 350
        assert almacen.sumas == [350]

    def test_con_exencion_explicita_el_paso_directo_sin_precio_se_ejecuta(self, monkeypatch):
        monkeypatch.setenv("NK_MODEL_CLAUDE_SONNET", "claude-sonnet-9")
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        _agente_critico(monkeypatch)
        red = Red(("api.anthropic.com", None, (200, _anthropic())))

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt="respaldo-d-a-sin-precio", http_post=red)

        assert res.provider == "anthropic-direct"
        assert res.usage_event.cost_source is CostSource.UNAVAILABLE
        assert res.usage_event.cost_micros is None

    def test_override_gemini_sin_precio_salta_el_directo_con_guard(self, monkeypatch):
        _todas_las_claves(monkeypatch)
        monkeypatch.setenv("NK_MODEL_GEMINI_PRO", "gemini-3-pro")
        red = Red(("openrouter.ai", None, (200, _chat_con_coste())))
        guard, almacen = _guard()

        res = llm.complete_for("contexto_largo", _MSG, guard=guard, http_post=red)

        assert red.modelos() == ["anthropic/claude-sonnet-5.5"]
        assert res.provider == "openrouter:anthropic"
        assert almacen.sumas == [12300]

    def test_openrouter_sin_coste_nunca_suma_cero_y_avisa(self, monkeypatch, caplog):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat())))
        guard, almacen = _guard()

        with caplog.at_level(logging.WARNING, logger="nickname_common.llm"):
            res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard,
                                   http_post=red)

        assert res.usage_event.cost_source is CostSource.UNAVAILABLE
        assert res.usage_event.cost_micros is None
        assert almacen.sumas == [], "None no se registra como 0"
        assert "anomalía" in caplog.text

    def test_razonamiento_de_gemini_cuenta_como_salida(self, monkeypatch):
        """Google cobra thoughtsTokenCount a precio de salida y
        candidatesTokenCount no lo incluye (gemini_pro razona siempre)."""
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(("generativelanguage.googleapis.com", None, (200, _gemini(
            promptTokenCount=1000, candidatesTokenCount=100, thoughtsTokenCount=5000,
            totalTokenCount=6100))))
        guard, almacen = _guard()

        res = llm.complete_for("contexto_largo", _MSG, guard=guard, http_post=red)

        esperado = estimate_cost_micros("gemini-3.1-pro-preview", input_tokens=1000,
                                        output_tokens=5100)
        assert esperado == 63_200  # 1000×2 + 5100×12 USD/M (gemini-3.1-pro-preview)
        assert res.usage_event.cost_micros == esperado
        assert res.usage_event.output_tokens == 5100
        assert almacen.sumas == [esperado]

    def test_gemini_solo_razonamiento_sin_texto_tambien_cuenta(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        cuerpo = {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
                  "usageMetadata": {"promptTokenCount": 1000, "thoughtsTokenCount": 1024,
                                    "totalTokenCount": 2024}}
        red = Red(("generativelanguage.googleapis.com", None, (200, cuerpo)))
        guard, almacen = _guard()

        res = llm.complete_for("contexto_largo", _MSG, guard=guard, http_post=red)

        assert res.usage_event.output_tokens == 1024
        assert almacen.sumas == [estimate_cost_micros(
            "gemini-3.1-pro-preview", input_tokens=1000, output_tokens=1024)]

    def test_tope_por_llamada_superado_conserva_la_respuesta(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(coste=0.5))))
        guard, almacen = _guard(por_llamada=100_000)

        with pytest.raises(BudgetExhausted) as exc:
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard,
                             json_schema=_SCHEMA, http_post=red)

        assert exc.value.reason is BudgetBlockedReason.PER_CALL_EXCEEDED
        assert almacen.sumas == [500_000], "lo gastado se registra igualmente"
        assert isinstance(exc.value.result, llm.LLMResult)
        assert exc.value.result.parsed_json == {"ok": True}
        assert exc.value.usage_event.cost_micros == 500_000

    def test_tope_por_llamada_respetado_no_lanza(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(coste=0.01))))
        guard, almacen = _guard(por_llamada=100_000)
        llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)
        assert almacen.sumas == [10_000]

    def test_byok_suma_comision_y_coste_upstream(self, monkeypatch):
        """D-A: con BYOK `cost` es la comisión de OpenRouter (en sus créditos)
        y el proveedor factura aparte upstream_inference_cost (crédito Team):
        el gasto real es la suma (ejemplo de la documentación de OpenRouter)."""
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(
            coste=0.95, is_byok=True, cost_details={"upstream_inference_cost": 19}))))
        guard, almacen = _guard(limite=100_000_000)

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        assert res.usage["upstream_cost_micros"] == 19_000_000
        assert res.usage["is_byok"] == 1
        assert res.usage_event.cost_micros == 19_950_000
        assert almacen.sumas == [19_950_000]

    def test_byok_sin_comision_es_solo_upstream(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(
            coste=0, is_byok=True, cost_details={"upstream_inference_cost": 0.02}))))
        guard, almacen = _guard()

        llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        assert almacen.sumas == [20_000]

    def test_sin_byok_upstream_igual_no_cuenta_dos_veces(self, monkeypatch):
        """Recibo real (docs/evidence/flash-lite-cost-20260913.json): sin BYOK
        upstream_inference_cost repite el mismo importe que cost."""
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(
            coste=0.02, is_byok=False, cost_details={"upstream_inference_cost": 0.02}))))
        guard, almacen = _guard()

        llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        assert almacen.sumas == [20_000]

    def test_sin_marca_byok_toma_el_mayor_sin_contar_dos_veces(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(
            coste=0.001, cost_details={"upstream_inference_cost": 0.02}))))
        guard, almacen = _guard()

        llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        assert almacen.sumas == [20_000]

    def test_byok_sin_upstream_avisa(self, monkeypatch, caplog):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(coste=0.001, is_byok=True))))
        guard, almacen = _guard()

        with caplog.at_level(logging.WARNING, logger="nickname_common.llm"):
            llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=guard, http_post=red)

        assert almacen.sumas == [1_000]
        assert "BYOK" in caplog.text


# --- Evento de uso ------------------------------------------------------------

class TestEventoDeUso:
    def test_evento_coherente_y_sin_contenido(self, monkeypatch):
        _router(monkeypatch)
        monkeypatch.setenv(llm.ROUTER_AGENT_ENV, "nickname-facturas-agent")
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(f'{{"eco": "{_SECRETO}"}}'))))

        res = llm.complete_for("cliente_dinero_tiempo_real", _MSG, guard=None,
                               budget_exempt="job-sin-store", http_post=red)

        ev = res.usage_event
        assert isinstance(ev, LLMUsageEventV1)
        assert ev.service == "nickname-facturas-agent"
        assert ev.job_id == "ai_policy:cliente_dinero_tiempo_real"
        assert ev.run_id
        assert ev.reason == "clase:cliente_dinero_tiempo_real;budget_exempt:job-sin-store"
        assert ev.provider == "openrouter:anthropic"
        assert ev.requested_model == "anthropic/claude-sonnet-5.5"
        assert ev.outcome is LLMOutcome.SERVED
        assert ev.input_tokens == 10
        assert ev.output_tokens == 5
        assert ev.cost_micros == 12300
        assert ev.cost_source is CostSource.PROVIDER_RESPONSE
        assert _SECRETO not in json.dumps(ev.to_dict()), "sin prompt ni respuesta"

    def test_identificadores_explicitos(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(("generativelanguage.googleapis.com", None, (200, _gemini())))
        res = llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red,
                               service="svc", job_id="job", run_id="run-1",
                               reason="triaje")
        ev = res.usage_event
        assert (ev.service, ev.job_id, ev.run_id) == ("svc", "job", "run-1")
        assert ev.reason == f"triaje;budget_exempt:{_EXENTO}"

    def test_run_id_por_defecto_es_unico(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(("generativelanguage.googleapis.com", None, (200, _gemini())))
        a = llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)
        b = llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                             budget_exempt=_EXENTO, http_post=red)
        assert a.usage_event.run_id != b.usage_event.run_id

    def test_servicio_sin_identificar(self, monkeypatch):
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(("generativelanguage.googleapis.com", None, (200, _gemini())))
        res = llm.complete_for("clasificacion_masiva", _MSG, guard=None,
                               budget_exempt=_EXENTO, http_post=red)
        assert res.usage_event.service == "nickname-sin-identificar"


# --- complete() legado intacto ------------------------------------------------

class TestLegadoIntacto:
    def test_complete_no_adjunta_evento(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat_con_coste(
            cost_details={"upstream_inference_cost": 0.5}))))

        res = llm.complete("gemini_flash", [{"role": "user", "content": "x"}], http_post=red)

        assert res.usage_event is None
        assert "upstream_cost_micros" not in res.usage, "el usage legado no cambia"
        assert red.llamadas[0]["body"]["temperature"] == 0.0
        assert red.llamadas[0]["body"]["max_tokens"] == 16

    def test_fallback_anthropic_legado_sigue_con_schema_por_instruccion(self, monkeypatch):
        _router(monkeypatch)
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        red = Red(
            ("openrouter.ai", None, (500, {})),
            ("api.anthropic.com", None, (200, _anthropic())),
        )

        res = llm.complete("gemini_flash", [{"role": "user", "content": "x"}],
                           json_schema=_SCHEMA, http_post=red)

        cuerpo = red.llamadas[1]["body"]
        assert "output_config" not in cuerpo
        assert "JSON Schema" in cuerpo["system"]
        assert cuerpo["temperature"] == 0.0
        assert cuerpo["model"] == llm.ANTHROPIC_FALLBACK_MODEL_DEFAULT
        assert res.provider == "anthropic-fallback"

    def test_fallback_openai_legado_sigue_con_max_tokens(self, monkeypatch):
        _router(monkeypatch)
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        red = Red(
            ("openrouter.ai", None, (500, {})),
            ("api.openai.com", None, (200, _chat())),
        )

        res = llm.complete("gemini_flash", [{"role": "user", "content": "x"}], http_post=red)

        cuerpo = red.llamadas[1]["body"]
        assert cuerpo["max_tokens"] == 16
        assert "max_completion_tokens" not in cuerpo
        assert cuerpo["temperature"] == 0.0
        assert cuerpo["model"] == llm.OPENAI_FALLBACK_MODEL_DEFAULT
        assert res.provider == "openai-fallback"

    def test_gemini_directo_legado_no_anade_razonamiento_al_usage(self, monkeypatch):
        _router(monkeypatch)
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-test")
        red = Red(
            ("openrouter.ai", None, (500, {})),
            ("generativelanguage.googleapis.com", None,
             (200, _gemini(thoughtsTokenCount=50))),
        )

        res = llm.complete("gemini_flash", [{"role": "user", "content": "x"}], http_post=red)

        assert res.provider == "google-direct"
        assert "reasoning_tokens" not in res.usage
        assert res.usage == {"prompt_tokens": 700, "completion_tokens": 300,
                             "total_tokens": 1000}

    def test_complete_legado_tambien_aplica_el_guard_al_slug(self, monkeypatch):
        """El guard L-014 del slug vive en router_slug: complete() lo hereda."""
        _router(monkeypatch)
        monkeypatch.setenv("NK_MODEL_GEMINI_FLASH_SLUG", "openai/gpt-6-astra")
        red = Red(("openrouter.ai", None, (200, _chat())))
        with pytest.raises(llm.CrossProviderOverrideError):
            llm.complete("gemini_flash", [{"role": "user", "content": "x"}], http_post=red)
        assert red.llamadas == []

    def test_claude_sonnet_legado_enruta_al_slug_reparado(self, monkeypatch):
        _router(monkeypatch)
        red = Red(("openrouter.ai", None, (200, _chat())))
        res = llm.complete("claude_sonnet", [{"role": "user", "content": "x"}], http_post=red)
        assert red.llamadas[0]["body"]["model"] == "anthropic/claude-sonnet-5.5"
        assert res.provider == "openrouter:anthropic"
