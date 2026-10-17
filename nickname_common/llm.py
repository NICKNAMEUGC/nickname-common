"""Registro central de modelos LLM del ecosistema Nickname (F0.1 Quality Leap 2026-06).

Fuente única de verdad para nombres de modelo y configs de generación.
Nace del incidente L-014: gemini-2.0-flash retirado por Google dejó 9
servicios/pipelines rotos en silencio durante semanas porque cada repo
hardcodeaba el string del modelo.

Con este registro:
  - Retirada de un modelo = actualizar _DEFAULTS aquí + bump del pin en los repos.
  - Override de EMERGENCIA por servicio sin tocar código: env var en Railway.
        NK_MODEL_GEMINI_FLASH=gemini-3-flash
  - El canary de NightWatch (task 15) vigila a diario que los modelos
    de este registro siguen vivos en el proveedor.

Uso:
    from nickname_common.llm import get_model, gemini_config_sdk, gemini_config_rest

    model = get_model("gemini_flash")

    # SDK google-genai (dict aceptado por GenerateContentConfig):
    client.models.generate_content(
        model=model, contents=prompt,
        config=gemini_config_sdk(max_output_tokens=1024),
    )

    # REST v1beta (camelCase):
    body = {"contents": [...], "generationConfig": gemini_config_rest(maxOutputTokens=1024)}

Fase 1 xAI (2026-08-23): tiers `grok_fast` / `grok_quality` + `complete()`.
No sustituyen a Gemini. `NK_MODEL_GEMINI_*` no puede apuntar a `grok-*`
(guard L-014). Sin `XAI_API_KEY` o sin `NK_XAI_LIVE=1`, `complete()` falla
cerrado y no llama a la red.

Cascada multi-proveedor (mandato Diego 2026-08-31, `.ag/decisions_log.md` →
LLM-FALLBACK-OPENROUTER-MANDATORY): con `NK_ROUTER=openrouter`, un fallo de
TRANSPORTE del enrutador ya no para el pipeline — `complete()` cae al
siguiente proveedor DISPONIBLE (con credencial):

    openrouter → gemini_directo (solo tiers gemini) → openai → anthropic

Esto SUPERSEDE deliberadamente la filosofía 2026-08-25 de "no se cae al
proveedor directo a propósito": disponibilidad gana; el control de gasto se
conserva midiendo y etiquetando cada leg (`LLMResult.provider` dice por qué
puerta salió cada respuesta). Solo errores de transporte encadenan
(HTTP >=400, red, timeout); un 200 con JSON que no parsea NO se relitiga en
otro proveedor. Los legs openai/anthropic CRUZAN de familia por diseño del
mandato de disponibilidad — el guard L-014 (`CrossProviderOverrideError`) es
de OVERRIDES de env (`NK_MODEL_<TIER>`), no de la cascada.

Regla L-014 incorporada: thinking_budget=0 por defecto en los tiers flash —
en Gemini 2.5 el razonamiento consume max_output_tokens ANTES del output y
trunca JSON/respuestas con caps ajustados. Pasar thinking_budget=None para
permitir thinking dinámico (tiers de calidad, ej. redacción con gemini_pro).

GOTCHA (verificado 2026-06-12): gemini-2.5-pro RECHAZA thinking_budget=0 con
400 "Budget 0 is invalid. This model only works in thinking mode". Para el
tier gemini_pro usar SIEMPRE thinking_budget=None.

Tier `gemini_flash_lite` (2026-09-11, SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911
WI-1): jobs Railway recurrentes de bajo coste — clasificar un delta ya acotado,
resumir comentarios anonimizados. Misma familia y mismo manejo de
thinking_budget que gemini_flash (0 por defecto). NO sustituye a gemini_flash
en pipelines que ya pasan su eval con ese tier: cambiar de tier un consumidor
existente exige revalidar su eval, igual que cualquier cambio de modelo. El
slug de OpenRouter sigue el mismo patrón que los demás tiers Gemini
(`google/<model-id>`); el canary diario de NightWatch (task 15) lo confirma
vivo igual que al resto del registro. Coste, presupuesto y atribución por
llamada de este tier viven en `nickname_common.budget` /
`nickname_common.models.llm_usage` / `nickname_common.cost_catalog`, no aquí:
este módulo solo resuelve NOMBRE de modelo y transporte.

Plano IA, ola 1 (SPEC-AI-PLANE-20261017, decisiones D1/D2/D-A de Diego del
2026-10-17): `complete_for(clase, ...)` es la puerta NUEVA. Recorre la cadena
API que declara `nickname_common.ai_policy` (fuente canónica de la política
por clase de trabajo), con presupuesto obligatorio (`BudgetGuard` o
`budget_exempt` explícito) y un `LLMUsageEventV1` adjunto al resultado.
`complete()` se conserva tal cual para los consumidores existentes; el código
nuevo debe usar `complete_for`. Tiers añadidos en la ola 1: `claude_opus`,
`claude_haiku`, `gpt_quality`, `gpt_fast` y `gemini_embedding` (este último
no enrutable, como `imagen`).
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from nickname_common.budget import BudgetExhausted, BudgetGuard
from nickname_common.cost_catalog import estimate_cost_micros
from nickname_common.models.llm_usage import CostSource, LLMOutcome, LLMUsageEventV1

# Logger de módulo (stdlib): anomalías de config y fallos de leg de la cascada.
# NUNCA loguea contenido (prompts, respuestas, bodies) — solo proveedor,
# clase de error y HTTP status.
_LOG = logging.getLogger("nickname_common.llm")

# Defaults vigentes (verificados por API el 2026-10-17: GET /v1/models de
# Anthropic, de OpenAI y de Google AI Studio, más el listado público de
# OpenRouter GET /api/v1/models; los ids directos de xAI NO son verificables
# porque no hay XAI_API_KEY — los tiers grok_* siguen en modo dry y su nombre
# directo es el slug de OpenRouter sin el prefijo `x-ai/`).
# Histórico de retiradas: gemini-2.0-flash (Google, ~05/2026, L-014);
# imagen-4.0-generate-001 (Google, ~07/2026, L-037 — canary 15_model_canary,
# 404 en :predictImages, endpoint retirado por completo);
# claude-sonnet-4-20250514 (Anthropic) y su slug anthropic/claude-sonnet-4
# (OpenRouter), ya no listados el 2026-10-17; x-ai/grok-3-mini y x-ai/grok-4
# (OpenRouter), ya no listados el 2026-10-17. Sustituirlos es una REPARACIÓN
# (el modelo ya no existe), no un cambio de calidad: el refresco de calidad
# de los demás tiers es la decisión D-E, servicio a servicio con su eval.
_DEFAULTS = {
    # Gemini
    "gemini_flash": "gemini-2.5-flash",       # clasificación, triaje, alto volumen
    "gemini_flash_lite": "gemini-2.5-flash-lite",  # jobs Railway de bajo coste (SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911): clasificación de un delta acotado, resumen pequeño de comentarios anonimizados. NUNCA redacción que deba superar un eval de calidad — para eso gemini_flash.
    "gemini_pro": "gemini-2.5-pro",           # redacción de calidad (drafts voice-cloned)
    # NanoBanana (n8n LinkedIn/newsletter). ⚠️ Cambio de familia de API respecto al
    # anterior imagen-4.0-generate-001: este modelo se llama via :generateContent
    # (mismo endpoint que los tiers de texto) con
    # generationConfig.responseModalities=["TEXT","IMAGE"], NO via :predictImages
    # (API Vertex-Imagen clásica). Cualquier caller que siga usando el shape
    # instances/parameters de :predictImages debe migrar al shape contents/parts.
    # NINGÚN caller Python de este repo invoca hoy get_model("imagen") para generar
    # de verdad — la generación real vive en el workflow n8n "V3 LinkedIn Daily" /
    # Newsletter (HTTP node propio, fuera de este registro). Actualizar ese nodo es
    # un paso APARTE, pendiente (ver T-REVIEW-NIGHTWATCH-4c02d0c0).
    "imagen": "gemini-3.1-flash-image",
    # Embeddings de Gemini (RAG de Nicky, ig). Se llama por :embedContent, no
    # por chat: no se enruta por OpenRouter ni pasa por complete()/complete_for.
    "gemini_embedding": "gemini-embedding-001",
    # Anthropic. claude_sonnet: judge IG agent, n8n Content Factory y primario
    # de la clase cliente_dinero_* (D2: Claude/GPT de gama alta para todo lo
    # que toque clientes o dinero). Ojo: la familia 5.x rechaza `temperature`
    # (Opus 5.5 siempre; Sonnet/Haiku 5.5 con valores no default) — por eso
    # complete_for no la envía salvo que el consumidor la pida.
    "claude_sonnet": "claude-sonnet-5-5",
    "claude_opus": "claude-opus-5-5",
    "claude_haiku": "claude-haiku-5-5",
    # OpenAI (respaldo de gama alta de cliente_dinero_*). Modelos de
    # razonamiento: en la API directa exigen max_completion_tokens.
    "gpt_quality": "gpt-6-astra",
    "gpt_fast": "gpt-5.4-mini",
    # xAI — opt-in. NUNCA alias de gemini_flash/gemini_pro (L-014).
    # No hay paridad de embeddings ni de generación de imagen.
    # grok_fast: el grok GENERALISTA vigente más barato según el `pricing`
    # de OpenRouter el 2026-10-17 (empata con grok-4.20; se elige el más
    # reciente). grok-build-0.1 es más barato pero es un modelo de código
    # especializado, y grok-4.20-multi-agent queda fuera por ser multi-agente.
    "grok_fast": "grok-4.3",
    "grok_quality": "grok-4.7",
}

ENV_PREFIX = "NK_MODEL_"
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
XAI_LIVE_ENV = "NK_XAI_LIVE"
XAI_KEY_ENV = "XAI_API_KEY"

# --- Enrutador (OpenRouter) ---------------------------------------------------
# Un unico punto de control de gasto, eleccion de modelo por tarea y libertad
# para cambiar de proveedor sin tocar cada repo. Contratado por Diego el
# 2026-08-25 tras la auditoria del gasto de Gemini.
#
# FAIL-SAFE: por defecto NO enruta. Encender es explicito y por servicio
# (NK_ROUTER=openrouter), asi que ningun agente cambia de camino por accidente.
ROUTER_ENV = "NK_ROUTER"
ROUTER_KEY_ENV = "OPENROUTER_API_KEY"
ROUTER_AGENT_ENV = "NK_AGENT_NAME"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# tier -> slug de OpenRouter. La familia se conserva: enrutar NO es cambiar de
# modelo a escondidas. Cambiar de modelo es una decision aparte y explicita.
_ROUTER_SLUGS = {
    "gemini_flash": "google/gemini-2.5-flash",
    "gemini_flash_lite": "google/gemini-2.5-flash-lite",
    "gemini_pro": "google/gemini-2.5-pro",
    "claude_sonnet": "anthropic/claude-sonnet-5.5",
    "claude_opus": "anthropic/claude-opus-5.5",
    "claude_haiku": "anthropic/claude-haiku-5.5",
    "gpt_quality": "openai/gpt-6-astra",
    "gpt_fast": "openai/gpt-5.4-mini",
    "grok_fast": "x-ai/grok-4.3",
    "grok_quality": "x-ai/grok-4.7",
}

# Tiers que NO pueden pasar por el enrutador. No es pereza: OpenRouter es un
# API de chat. Ni embeddings ni generacion de imagen tienen paridad, y colarlos
# romperia el RAG de Nicky y el carrusel de LinkedIn en silencio.
_ROUTER_UNSUPPORTED = {"imagen", "gemini_embedding"}

# --- Cascada de fallback multi-proveedor (mandato 2026-08-31) -----------------
# Legs directos que se intentan cuando el transporte OpenRouter falla. Cada leg
# solo se intenta si su credencial existe; sin credencial se salta al siguiente.
GEMINI_KEY_ENV = "GEMINI_API_KEY"
GEMINI_KEY_FALLBACK_ENV = "GOOGLE_AI_API_KEY"
GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
OPENAI_KEY_ENV = "OPENAI_API_KEY"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_FALLBACK_MODEL_ENV = "NK_MODEL_OPENAI_FALLBACK"
OPENAI_FALLBACK_MODEL_DEFAULT = "gpt-4o-mini"
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"  # nombre de var para os.getenv, jamás el valor
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_FALLBACK_MODEL_ENV = "NK_MODEL_ANTHROPIC_FALLBACK"
ANTHROPIC_FALLBACK_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
ANTHROPIC_VERSION = "2023-06-01"

# Claves de JSON Schema que el responseSchema de Gemini REST no soporta.
# Se limpian recursivamente antes de enviar el schema al leg gemini_directo.
_GEMINI_SCHEMA_DROP = {
    "additionalProperties",
    "$schema",
    "$id",
    "$defs",
    "definitions",
    "strict",
    "examples",
    "default",
    "title",
}

_FAMILY_PREFIXES = {
    "gemini_flash": ("gemini-",),
    "gemini_flash_lite": ("gemini-",),
    "gemini_pro": ("gemini-",),
    "imagen": ("gemini-", "imagen-"),
    # Solo embeddings de Gemini: `text-embedding-*` NO entra porque es también
    # el nombre de los embeddings de OpenAI (sería un cruce de proveedor).
    "gemini_embedding": ("gemini-embedding-",),
    "claude_sonnet": ("claude-",),
    "claude_opus": ("claude-",),
    "claude_haiku": ("claude-",),
    "gpt_quality": ("gpt-",),
    "gpt_fast": ("gpt-",),
    "grok_fast": ("grok-",),
    "grok_quality": ("grok-",),
}

# Capacidades de un tier de chat de texto sin embeddings ni imagen (Claude,
# GPT, Grok). Se copia por tier para que cambiar uno no arrastre a los demás.
_CHAT_ONLY = {
    "text": True,
    "json_schema": True,
    "embeddings": False,
    "images": False,
    "thinking_budget": False,
}

_CAPABILITIES = {
    "gemini_flash": {
        "text": True,
        "json_schema": True,
        "embeddings": True,
        "images": False,
        "thinking_budget": True,
    },
    "gemini_flash_lite": {
        "text": True,
        "json_schema": True,
        "embeddings": True,
        "images": False,
        "thinking_budget": True,
    },
    "gemini_pro": {
        "text": True,
        "json_schema": True,
        "embeddings": True,
        "images": False,
        "thinking_budget": True,
    },
    "imagen": {
        "text": False,
        "json_schema": False,
        "embeddings": False,
        "images": True,
        "thinking_budget": False,
    },
    "gemini_embedding": {
        "text": False,
        "json_schema": False,
        "embeddings": True,
        "images": False,
        "thinking_budget": False,
    },
    "claude_sonnet": {
        "text": True,
        "json_schema": True,
        "embeddings": False,
        "images": False,
        "thinking_budget": False,
    },
    "claude_opus": dict(_CHAT_ONLY),
    "claude_haiku": dict(_CHAT_ONLY),
    "gpt_quality": dict(_CHAT_ONLY),
    "gpt_fast": dict(_CHAT_ONLY),
    "grok_fast": {
        "text": True,
        "json_schema": True,
        "embeddings": False,
        "images": False,
        "thinking_budget": False,
    },
    "grok_quality": {
        "text": True,
        "json_schema": True,
        "embeddings": False,
        "images": False,
        "thinking_budget": False,
    },
}

HttpPost = Callable[[str, Mapping[str, str], dict, float], tuple[int, str]]


class CrossProviderOverrideError(ValueError):
    """NK_MODEL_<TIER> apuntó a un modelo de otra familia (guard L-014)."""


class ProviderNotConfigured(RuntimeError):
    """El proveedor no está listo (sin key, live gate, o transport)."""


class ClassNotApiExecutable(ValueError):
    """La clase de trabajo no se puede servir por `complete_for`.

    Dos casos, ambos errores de programación (por eso ValueError y no
    ProviderNotConfigured, que un consumidor podría tratar como "proveedor
    caído, degradar"): la clase es del carril suscripción y no declara cadena
    API (usar el broker de suscripciones, ola 4), o la cadena es de un tier
    que no es de chat (embeddings, imagen: van por su propio endpoint).
    """


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str
    model: str
    http_status: int
    parsed_json: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=dict)
    # Solo lo rellena complete_for (ola 1 del plano IA). complete() lo deja en
    # None, así que ningún consumidor existente ve un cambio.
    usage_event: LLMUsageEventV1 | None = None


def get_model(tier: str) -> str:
    """Modelo efectivo del tier: env var NK_MODEL_<TIER> > default del registro."""
    if tier not in _DEFAULTS:
        raise KeyError(
            f"Tier de modelo desconocido: {tier!r}. Válidos: {sorted(_DEFAULTS)}"
        )
    model = os.getenv(ENV_PREFIX + tier.upper(), _DEFAULTS[tier])
    _assert_model_family(tier, model)
    return model


def all_models() -> dict:
    """Mapa tier → modelo efectivo (overrides aplicados). Lo consume el canary."""
    return {tier: get_model(tier) for tier in _DEFAULTS}


def provider_of(tier: str) -> str:
    """Proveedor del tier: 'gemini' | 'anthropic' | 'openai' | 'xai'."""
    if tier not in _DEFAULTS:
        raise KeyError(f"Tier de modelo desconocido: {tier!r}")
    if tier.startswith("claude"):
        return "anthropic"
    if tier.startswith("gpt"):
        return "openai"
    if tier.startswith("grok"):
        return "xai"
    return "gemini"


def all_tiers() -> tuple[str, ...]:
    """Tiers del registro, sin resolver modelos (no aplica overrides de env)."""
    return tuple(_DEFAULTS)


def is_routable(tier: str) -> bool:
    """¿Puede el tier pasar por OpenRouter? Embeddings e imagen nunca."""
    if tier not in _DEFAULTS:
        raise KeyError(f"Tier de modelo desconocido: {tier!r}")
    return tier not in _ROUTER_UNSUPPORTED


def capabilities(tier: str) -> dict[str, bool]:
    """Matriz de capacidades del tier. Grok no sustituye embeddings ni imagen."""
    if tier not in _CAPABILITIES:
        raise KeyError(f"Tier de modelo desconocido: {tier!r}")
    return dict(_CAPABILITIES[tier])


def gemini_config_sdk(thinking_budget=0, **kwargs) -> dict:
    """Config para el SDK google-genai (snake_case; el SDK acepta dicts).

    thinking_budget=0 (default) = paridad 2.0 / proteger max_output_tokens (L-014).
    thinking_budget=None = thinking dinámico del modelo (tiers de calidad).
    """
    cfg = dict(kwargs)
    if thinking_budget is not None:
        cfg["thinking_config"] = {"thinking_budget": thinking_budget}
    return cfg


def gemini_config_rest(thinking_budget=0, **kwargs) -> dict:
    """generationConfig para la API REST v1beta (camelCase).

    Mismas semánticas de thinking_budget que gemini_config_sdk().
    """
    cfg = dict(kwargs)
    if thinking_budget is not None:
        cfg["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    return cfg


class RouterNotAvailable(ProviderNotConfigured):
    """El tier no puede enrutarse (embeddings/imagen) o falta la key."""


class CascadeExhausted(ProviderNotConfigured):
    """Todos los legs de la cascada fallaron o no tenían credencial.

    El mensaje resume la cadena de intentos (proveedor + clase de error +
    HTTP status), nunca contenido. Hereda de ProviderNotConfigured para que
    los consumidores que ya capturan esa clase sigan funcionando.

    `usage_event`: solo lo rellena complete_for (LLMUsageEventV1 con
    outcome=transport_error, sin coste); en la cascada legada queda en None.
    """

    usage_event: LLMUsageEventV1 | None = None


class _TransportFailure(Exception):
    """Fallo de transporte de un leg (HTTP >=400). Interno: encadena legs."""

    def __init__(self, reason: str, http_status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


def router_enabled() -> bool:
    """¿Este servicio enruta? Fail-safe: sin la var, va directo al proveedor."""
    return os.getenv(ROUTER_ENV, "").strip().lower() == "openrouter"


def router_slug(tier: str) -> str:
    """Slug de OpenRouter para el tier. Conserva familia y modelo."""
    if tier not in _DEFAULTS:
        raise KeyError(f"Tier de modelo desconocido: {tier!r}")
    if tier in _ROUTER_UNSUPPORTED:
        raise RouterNotAvailable(
            f"El tier {tier!r} no se enruta: OpenRouter es un API de chat y no hay "
            "paridad de embeddings ni de generacion de imagen. Sigue yendo directo."
        )
    slug = os.getenv(ENV_PREFIX + tier.upper() + "_SLUG", _ROUTER_SLUGS[tier])
    return slug


def _router_headers(key: str) -> dict[str, str]:
    """Cabeceras del enrutador.

    `X-Title` viaja con el nombre del agente: OpenRouter lo usa para desglosar el
    gasto POR AGENTE en su panel, que es justo lo que la facturacion de Google no
    daba. Sin NK_AGENT_NAME el gasto se mezcla y el desglose no sirve de nada.
    """
    agente = os.getenv(ROUTER_AGENT_ENV, "").strip() or "nickname-sin-identificar"
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": agente,
        "HTTP-Referer": "https://nickname.com",
    }


def complete(
    tier: str,
    messages: list[Mapping[str, str]],
    *,
    json_schema: dict[str, Any] | None = None,
    max_tokens: int = 16,
    temperature: float = 0.0,
    http_post: HttpPost | None = None,
    timeout: float = 30.0,
) -> LLMResult:
    """Completa un chat.

    Con `NK_ROUTER=openrouter` (enrutado) aplica la cascada del mandato
    LLM-FALLBACK-OPENROUTER-MANDATORY (2026-08-31):

        openrouter → gemini_directo (solo tiers gemini) → openai → anthropic

    - Cada leg solo se intenta si su credencial existe; sin credencial se
      salta al siguiente (la falta de OPENROUTER_API_KEY con el router
      encendido se registra como anomalía de config vía logger).
    - Solo errores de TRANSPORTE encadenan (HTTP >=400, red, timeout). Un 200
      con JSON que no parsea se devuelve como siempre (`parsed_json=None`).
    - `LLMResult.provider` etiqueta el leg real: "openrouter:google",
      "google-direct", "openai-fallback", "anthropic-fallback".
    - Si TODOS los legs fallan: `CascadeExhausted` con la cadena de intentos
      (proveedor + clase de error + HTTP status), sin contenido.
    - Los legs openai/anthropic CRUZAN de familia POR DISEÑO del mandato de
      disponibilidad. El guard L-014 (`CrossProviderOverrideError`) protege
      los OVERRIDES de env (`NK_MODEL_<TIER>`), no esta cascada: aquí el
      cruce es explícito y queda etiquetado en `provider`.

    Sin enrutador, Fase 1: solo xAI. Gemini/Anthropic siguen en su SDK.
    Sin `http_post` inyectado el camino xAI requiere `XAI_API_KEY` y
    `NK_XAI_LIVE=1`; el default no llama a la red (Phase 1 dry / sin billing).

    LEGADO (plano IA, ola 1, 2026-10-17): esta función se conserva sin cambios
    de comportamiento para los consumidores que ya la usan. Todo código NUEVO
    debe usar `complete_for(clase, ...)`, que elige tier y vía según
    `nickname_common.ai_policy` y exige presupuesto declarado.
    """
    provider = provider_of(tier)
    if router_enabled():
        return _complete_routed(
            tier,
            messages,
            json_schema=json_schema,
            max_tokens=max_tokens,
            temperature=temperature,
            http_post=http_post,
            timeout=timeout,
        )

    if provider != "xai":
        raise NotImplementedError(
            f"complete() sin enrutador solo implementa xAI; {provider} sigue en "
            f"su SDK. Para enrutarlo: {ROUTER_ENV}=openrouter"
        )
    model = get_model(tier)
    key = os.getenv(XAI_KEY_ENV, "").strip()
    if http_post is None:
        if not key:
            raise ProviderNotConfigured(
                "XAI_API_KEY no está provisionada (adapter dry, Fase 1)"
            )
        if os.getenv(XAI_LIVE_ENV, "") != "1":
            raise ProviderNotConfigured(
                "llamadas live a xAI gated (NK_XAI_LIVE!=1); Fase 1 dry adapter"
            )
        http_post = _urllib_post
    headers = {
        "Authorization": f"Bearer {key or 'test'}",
        "Content-Type": "application/json",
    }

    body: dict[str, Any] = {
        "model": model,
        "messages": [dict(m) for m in messages],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_schema is not None:
        body["response_format"] = _openai_response_format(json_schema)

    status, raw = http_post(XAI_CHAT_URL, headers, body, timeout)
    if status != 200:
        raise ProviderNotConfigured(
            f"xAI chat/completions HTTP {status}: {raw[:200]}"
        )
    payload = json.loads(raw)
    text = _openai_style_text(payload)
    usage_raw = payload.get("usage") or {}
    usage = {
        k: int(usage_raw[k])
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        if k in usage_raw
    }
    return LLMResult(
        text=text,
        parsed_json=_parse_json_text(text, json_schema),
        provider=provider,
        model=model,
        http_status=status,
        usage=usage,
    )


# --- Cascada enrutada ---------------------------------------------------------

# Excepciones que cuentan como fallo de TRANSPORTE de un leg y encadenan al
# siguiente: HTTP >=400 (_TransportFailure), red/timeout (OSError cubre
# URLError, TimeoutError, ConnectionError, socket) y respuestas HTTP rotas
# (http.client.HTTPException, p.ej. IncompleteRead). Cualquier otra excepción
# —incluido un 200 cuyo envelope no es JSON— propaga sin relitigarse.
_TRANSPORT_ERRORS = (_TransportFailure, OSError, http.client.HTTPException)

_GEMINI_DIRECT_TIERS = ("gemini_flash", "gemini_flash_lite", "gemini_pro")


def _complete_routed(
    tier: str,
    messages: list[Mapping[str, str]],
    *,
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float,
    http_post: HttpPost | None,
    timeout: float,
) -> LLMResult:
    """Cascada openrouter → gemini_directo → openai → anthropic."""
    # El slug valida de paso que el tier sea enrutable (imagen/embeddings no).
    slug = router_slug(tier)
    post = http_post if http_post is not None else _urllib_post
    intentos: list[str] = []

    def _fallo(leg: str, exc: Exception) -> None:
        motivo = (
            exc.reason if isinstance(exc, _TransportFailure) else type(exc).__name__
        )
        intentos.append(f"{leg}({motivo})")
        _LOG.warning(
            "cascada LLM tier=%s: leg %s falló (%s); se intenta el siguiente",
            tier, leg, motivo,
        )

    # Leg 1 — OpenRouter (primario del servicio enrutado).
    leg_or = f"openrouter:{slug.split('/', 1)[0]}"
    if not os.getenv(ROUTER_KEY_ENV, "").strip():
        # Anomalía de config: el servicio pidió enrutar pero no tiene key.
        # El mandato de disponibilidad manda seguir la cascada, no parar.
        _LOG.warning(
            "%s=openrouter pero falta %s: anomalía de config; se salta el leg "
            "openrouter y sigue la cascada (LLM-FALLBACK-OPENROUTER-MANDATORY)",
            ROUTER_ENV, ROUTER_KEY_ENV,
        )
        intentos.append(f"{leg_or}(sin-credencial)")
    else:
        try:
            return _leg_openrouter(
                slug, leg_or, messages, json_schema, max_tokens, temperature,
                post, timeout,
            )
        except _TRANSPORT_ERRORS as exc:
            _fallo(leg_or, exc)

    # Leg 2 — Gemini directo: mismo modelo del tier por la puerta REST v1beta.
    # Solo aplica a tiers de familia gemini.
    if tier not in _GEMINI_DIRECT_TIERS:
        intentos.append("google-direct(no-aplica)")
    elif not _gemini_direct_key():
        intentos.append("google-direct(sin-credencial)")
    else:
        try:
            return _leg_gemini_direct(
                tier, messages, json_schema, max_tokens, temperature, post, timeout
            )
        except _TRANSPORT_ERRORS as exc:
            _fallo("google-direct", exc)

    # Leg 3 — OpenAI (cruce de familia por diseño; ver docstring de complete()).
    if not os.getenv(OPENAI_KEY_ENV, "").strip():
        intentos.append("openai-fallback(sin-credencial)")
    else:
        try:
            return _leg_openai(
                messages, json_schema, max_tokens, temperature, post, timeout
            )
        except _TRANSPORT_ERRORS as exc:
            _fallo("openai-fallback", exc)

    # Leg 4 — Anthropic (cruce de familia por diseño).
    if not os.getenv(ANTHROPIC_KEY_ENV, "").strip():
        intentos.append("anthropic-fallback(sin-credencial)")
    else:
        try:
            return _leg_anthropic(
                messages, json_schema, max_tokens, temperature, post, timeout
            )
        except _TRANSPORT_ERRORS as exc:
            _fallo("anthropic-fallback", exc)

    raise CascadeExhausted(
        f"Cascada LLM agotada para tier {tier!r} con {ROUTER_ENV}=openrouter. "
        "Intentos (proveedor/clase de error/HTTP): " + " -> ".join(intentos)
    )


# --- Plano IA, ola 1: complete_for (SPEC-AI-PLANE-20261017 §5) ---------------

# Etiqueta de LLMResult.provider para cada vía directa. La vía openrouter se
# etiqueta `openrouter:<familia del slug>` igual que en la cascada legada.
_DIRECT_VIA_LABELS = {
    "anthropic_direct": "anthropic-direct",
    "openai_direct": "openai-direct",
    "gemini_direct": "google-direct",
}

# Por defecto, sin agente identificado; igual que la cabecera X-Title.
_UNIDENTIFIED_SERVICE = "nickname-sin-identificar"


def complete_for(
    clase: str,
    messages: list[Mapping[str, str]],
    *,
    guard: BudgetGuard | None,
    budget_exempt: str | None = None,
    json_schema: dict[str, Any] | None = None,
    max_tokens: int = 1024,
    temperature: float | None = None,
    http_post: HttpPost | None = None,
    timeout: float = 30.0,
    service: str | None = None,
    job_id: str | None = None,
    run_id: str | None = None,
    reason: str | None = None,
) -> LLMResult:
    """Completa un chat según la política de la CLASE de trabajo.

    La clase (`nickname_common.ai_policy`, fuente canónica) fija la cadena
    ordenada de pasos `{tier, via}`. Se recorre en orden:

    - Paso `@openrouter`: solo si `NK_ROUTER=openrouter` (router_enabled()) Y
      hay `OPENROUTER_API_KEY`. Quitar `NK_ROUTER` sigue siendo la palanca de
      rollback: el servicio deja de salir por OpenRouter sin deploy.
    - Pasos directos (`anthropic_direct`, `openai_direct`, `gemini_direct`):
      solo si existe su clave. Por la decisión D-A, la clave Anthropic directa
      solo vive en los agentes críticos de dinero; en los demás ese paso se
      salta y queda en la lista de intentos.
    - Solo los fallos de TRANSPORTE encadenan (HTTP >=400, red, timeout). Un
      200 con JSON de contenido inválido se devuelve (`parsed_json=None`) y
      NO se relitiga en el paso siguiente.
    - Todo agotado → `CascadeExhausted` con la cadena de intentos (vía, tier,
      motivo), nunca contenido.

    Presupuesto OBLIGATORIO: `guard` es un `BudgetGuard`; con `guard=None`
    hay que declarar `budget_exempt="<motivo>"` (si no, ValueError). Con
    guard: `check_before_call()` antes del primer paso (`BudgetExhausted` se
    propaga sin tocar la red, con `exc.usage_event` tipado como
    budget_blocked); tras el éxito, `record_call(coste)` y
    `check_call_cost(coste)`. Si el coste real supera el tope por llamada, la
    llamada YA ocurrió: se propaga `BudgetExhausted(PER_CALL_EXCEEDED)` con la
    respuesta en `exc.result` y el evento en `exc.usage_event`, para que el
    consumidor lo trate como anomalía sin perder lo pagado.

    Coste: el que informa el proveedor (OpenRouter `usage.cost`; con BYOK el
    gasto real del modelo puede viajar aparte en
    `cost_details.upstream_inference_cost` y se toma el mayor de los dos para
    no infravalorar ni contar dos veces) → si no hay, el catálogo verificado
    (`cost_catalog`) → si tampoco, `None` (`cost_source=unavailable`, nunca 0).

    El resultado lleva `usage_event` (LLMUsageEventV1, sin PII ni prompt).
    `LLMResult.provider` etiqueta el paso real: `openrouter:anthropic`,
    `openrouter:openai`, `openrouter:google`, `anthropic-direct`,
    `openai-direct` o `google-direct`.

    `temperature=None` (default) no envía el parámetro: la familia Claude 5.x y
    los GPT de razonamiento lo rechazan. `max_tokens` por defecto 1024: los
    modelos con razonamiento lo consumen antes del texto visible.
    """
    exempt = _validated_budget_args(guard, budget_exempt)
    steps = _api_steps_for(clase)
    event_base = {
        "service": (
            service
            or os.getenv(ROUTER_AGENT_ENV, "").strip()
            or _UNIDENTIFIED_SERVICE
        ),
        "job_id": job_id or f"ai_policy:{clase}",
        "run_id": run_id or uuid.uuid4().hex,
        "reason": (reason or f"clase:{clase}")
        + (f";budget_exempt:{exempt}" if exempt else ""),
    }

    if guard is not None:
        try:
            guard.check_before_call()
        except BudgetExhausted as exc:
            exc.usage_event = LLMUsageEventV1.budget_blocked(
                requested_model=_step_requested_model(steps[0]), **event_base
            )
            raise

    post = http_post if http_post is not None else _urllib_post
    intentos: list[str] = []
    for step in steps:
        tier, via = step.tier, step.via
        if via == "openrouter":
            slug = router_slug(tier)
            label = f"openrouter:{slug.split('/', 1)[0]}"
            if not router_enabled():
                intentos.append(f"{label}[{tier}](router-apagado)")
                continue
            if not os.getenv(ROUTER_KEY_ENV, "").strip():
                _LOG.warning(
                    "%s=openrouter pero falta %s: anomalía de config; complete_for "
                    "salta el paso %s[%s]", ROUTER_ENV, ROUTER_KEY_ENV, label, tier,
                )
                intentos.append(f"{label}[{tier}](sin-credencial)")
                continue

            def call(slug=slug, label=label):
                return _leg_openrouter(
                    slug, label, messages, json_schema, max_tokens, temperature,
                    post, timeout, track_upstream_cost=True,
                )
        elif via == "anthropic_direct":
            label = _DIRECT_VIA_LABELS[via]
            if not os.getenv(ANTHROPIC_KEY_ENV, "").strip():
                intentos.append(f"{label}[{tier}](sin-credencial)")
                continue

            def call(tier=tier, label=label):
                return _leg_anthropic(
                    messages, json_schema, max_tokens, temperature, post, timeout,
                    model=get_model(tier), provider_label=label, native_schema=True,
                )
        elif via == "openai_direct":
            label = _DIRECT_VIA_LABELS[via]
            if not os.getenv(OPENAI_KEY_ENV, "").strip():
                intentos.append(f"{label}[{tier}](sin-credencial)")
                continue

            def call(tier=tier, label=label):
                return _leg_openai(
                    messages, json_schema, max_tokens, temperature, post, timeout,
                    model=get_model(tier), provider_label=label,
                    token_param="max_completion_tokens",
                )
        elif via == "gemini_direct":
            label = _DIRECT_VIA_LABELS[via]
            if not _gemini_direct_key():
                intentos.append(f"{label}[{tier}](sin-credencial)")
                continue

            def call(tier=tier):
                return _leg_gemini_direct(
                    tier, messages, json_schema, max_tokens, temperature, post,
                    timeout,
                )
        else:  # pragma: no cover — ai_policy valida las vías al importarse
            raise ValueError(f"Vía desconocida en la política: {via!r}")

        try:
            result = call()
        except _TRANSPORT_ERRORS as exc:
            motivo = (
                exc.reason if isinstance(exc, _TransportFailure) else type(exc).__name__
            )
            intentos.append(f"{label}[{tier}]({motivo})")
            _LOG.warning(
                "complete_for clase=%s: paso %s[%s] falló (%s); se intenta el siguiente",
                clase, label, tier, motivo,
            )
            continue
        return _settle_result(result, guard=guard, event_base=event_base)

    exhausted = CascadeExhausted(
        f"Cadena API agotada para la clase {clase!r}. "
        "Intentos (vía[tier](motivo)): " + " -> ".join(intentos)
    )
    exhausted.usage_event = LLMUsageEventV1(
        provider="none",
        requested_model=_step_requested_model(steps[0]),
        outcome=LLMOutcome.TRANSPORT_ERROR,
        cost_source=CostSource.UNAVAILABLE,
        **event_base,
    )
    raise exhausted


def _validated_budget_args(
    guard: BudgetGuard | None, budget_exempt: str | None
) -> str | None:
    """Presupuesto obligatorio: un BudgetGuard o una exención con motivo."""
    if budget_exempt is not None and not isinstance(budget_exempt, str):
        raise TypeError("budget_exempt debe ser un str con el motivo de la exención")
    exempt = (budget_exempt or "").strip()
    if guard is None:
        if not exempt:
            raise ValueError(
                "complete_for exige presupuesto declarado: pasa guard=BudgetGuard(...) "
                "o, si el servicio está exento, budget_exempt='<motivo>' explícito"
            )
        return exempt
    if not isinstance(guard, BudgetGuard):
        raise TypeError(
            f"guard debe ser un nickname_common.budget.BudgetGuard, no {type(guard).__name__}"
        )
    if budget_exempt is not None:
        raise ValueError("guard y budget_exempt son excluyentes: elige uno")
    return None


def _api_steps_for(clase: str) -> tuple:
    """Cadena API ejecutable de la clase, o ClassNotApiExecutable."""
    # Import diferido: ai_policy importa este módulo para validar sus tiers.
    from nickname_common import ai_policy

    steps = ai_policy.api_chain(clase)
    if not steps:
        raise ClassNotApiExecutable(
            f"La clase {clase!r} es del carril suscripción y no declara cadena API: "
            "se sirve por el broker de suscripciones del Mac Mini (ola 4), no por "
            "complete_for"
        )
    no_chat = sorted({s.tier for s in steps if not capabilities(s.tier)["text"]})
    if no_chat:
        raise ClassNotApiExecutable(
            f"La clase {clase!r} usa tiers que no son de chat ({', '.join(no_chat)}): "
            "se llaman por su endpoint propio con get_model(tier), no por complete_for"
        )
    return steps


def _step_requested_model(step) -> str:
    """Modelo que pediría el paso (slug en OpenRouter, id en directo)."""
    if step.via == "openrouter":
        return router_slug(step.tier)
    return get_model(step.tier)


def _result_cost(result: LLMResult) -> tuple[int | None, CostSource]:
    usage = result.usage
    reported = [
        usage[k] for k in ("cost_micros", "upstream_cost_micros") if usage.get(k) is not None
    ]
    if reported:
        # BYOK: `cost` puede ser solo la comisión y el gasto real del modelo
        # viajar en upstream; sin BYOK upstream falta o coincide. El mayor de
        # los dos no infravalora ni cuenta dos veces.
        return max(reported), CostSource.PROVIDER_RESPONSE
    # Catálogo: ids sin prefijo de OpenRouter (precio pass-through idéntico).
    estimated = estimate_cost_micros(
        result.model.split("/", 1)[-1],
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
    )
    if estimated is not None:
        return estimated, CostSource.CATALOG
    return None, CostSource.UNAVAILABLE


def _settle_result(
    result: LLMResult, *, guard: BudgetGuard | None, event_base: dict[str, str]
) -> LLMResult:
    cost, source = _result_cost(result)
    event = LLMUsageEventV1(
        provider=result.provider,
        requested_model=result.model,
        outcome=LLMOutcome.SERVED,
        input_tokens=result.usage.get("prompt_tokens"),
        output_tokens=result.usage.get("completion_tokens"),
        cost_micros=cost,
        cost_source=source,
        **event_base,
    )
    result = dataclasses.replace(result, usage_event=event)
    if guard is not None:
        guard.record_call(cost)  # None no suma: coste desconocido nunca es 0
        try:
            guard.check_call_cost(cost)
        except BudgetExhausted as exc:
            exc.result = result
            exc.usage_event = event
            raise
    return result


def _gemini_direct_key() -> str:
    return (
        os.getenv(GEMINI_KEY_ENV, "").strip()
        or os.getenv(GEMINI_KEY_FALLBACK_ENV, "").strip()
    )


def _openai_response_format(json_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "result",
            "schema": json_schema,
            "strict": True,
        },
    }


def _openai_style_text(payload: dict) -> str:
    choices = payload.get("choices") or [{}]
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _parse_json_text(
    text: str, json_schema: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Un 200 con JSON inválido NO encadena: se devuelve parsed_json=None."""
    if json_schema is None or not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _token_usage(usage_raw: Mapping[str, Any]) -> dict[str, int]:
    return {
        k: int(usage_raw[k])
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        if k in usage_raw
    }


def _usd_to_micros(value: Any) -> int:
    return int(round(float(value) * 1_000_000))


def _leg_openrouter(
    slug: str,
    leg: str,
    messages: list[Mapping[str, str]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float | None,
    post: HttpPost,
    timeout: float,
    *,
    track_upstream_cost: bool = False,
) -> LLMResult:
    """Leg OpenRouter. `temperature=None` no la envía (solo complete_for).

    `track_upstream_cost=True` (solo complete_for) añade a `usage` la clave
    `upstream_cost_micros` cuando OpenRouter informa
    `usage.cost_details.upstream_inference_cost`: con BYOK (decisión D-A, el
    crédito Team de Anthropic) `cost` puede ser solo la comisión de OpenRouter
    y el gasto real del modelo viaja en ese campo. complete() no lo pide, así
    que su `usage` no cambia.
    """
    key = os.getenv(ROUTER_KEY_ENV, "").strip()
    body: dict[str, Any] = {
        "model": slug,
        "messages": [dict(m) for m in messages],
        "max_tokens": max_tokens,
        # Sin esto OpenRouter devuelve tokens pero no coste, y el contador del
        # Command Center se quedaria otra vez sin la cifra que importa.
        "usage": {"include": True},
    }
    if temperature is not None:
        body["temperature"] = temperature
    if json_schema is not None:
        body["response_format"] = _openai_response_format(json_schema)

    status, raw = post(OPENROUTER_CHAT_URL, _router_headers(key), body, timeout)
    if status != 200:
        raise _TransportFailure(f"HTTP {status}", status)
    payload = json.loads(raw)
    text = _openai_style_text(payload)
    usage_raw = payload.get("usage") or {}
    usage = _token_usage(usage_raw)
    if usage_raw.get("cost") is not None:
        # Milesimas de dolar para no meter floats en un dict[str, int].
        usage["cost_micros"] = _usd_to_micros(usage_raw["cost"])
    if track_upstream_cost:
        details = usage_raw.get("cost_details") or {}
        upstream = details.get("upstream_inference_cost") if isinstance(details, dict) else None
        if upstream is not None:
            usage["upstream_cost_micros"] = _usd_to_micros(upstream)
    return LLMResult(
        text=text,
        parsed_json=_parse_json_text(text, json_schema),
        provider=leg,
        model=slug,
        http_status=status,
        usage=usage,
    )


def _gemini_schema(schema: Any) -> Any:
    """Adapta JSON Schema al subconjunto ``responseSchema`` de Gemini REST.

    Gemini representa ``T | null`` con ``type: T`` + ``nullable: true`` y
    rechaza el array JSON Schema ``type: [T, "null"]`` con HTTP 400.  Cuando
    el campo tiene ``enum``, ``null`` se expresa exclusivamente mediante
    ``nullable`` para que el enum conserve valores del mismo tipo.

    Las uniones de varios tipos no nulos se dejan intactas: Gemini las
    rechazará de forma visible en vez de degradar silenciosamente el contrato.
    """
    if isinstance(schema, dict):
        cleaned = {
            k: _gemini_schema(v)
            for k, v in schema.items()
            if k not in _GEMINI_SCHEMA_DROP
        }
        schema_type = cleaned.get("type")
        if isinstance(schema_type, list):
            non_null = [item for item in schema_type if item != "null"]
            if len(non_null) == 1 and len(schema_type) == 2:
                cleaned["type"] = non_null[0]
                cleaned["nullable"] = True
                enum = cleaned.get("enum")
                if isinstance(enum, list):
                    cleaned["enum"] = [item for item in enum if item is not None]
        return cleaned
    if isinstance(schema, list):
        return [_gemini_schema(item) for item in schema]
    return schema


def _gemini_contents(
    messages: list[Mapping[str, str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Convierte messages estilo chat a contents de Gemini (assistant→model)."""
    contents: list[dict[str, Any]] = []
    system_parts: list[str] = []
    for m in messages:
        role = str(m.get("role") or "user")
        text = str(m.get("content") or "")
        if role == "system":
            system_parts.append(text)
            continue
        contents.append({
            "role": "model" if role == "assistant" else "user",
            "parts": [{"text": text}],
        })
    return contents, system_parts


def _leg_gemini_direct(
    tier: str,
    messages: list[Mapping[str, str]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float | None,
    post: HttpPost,
    timeout: float,
) -> LLMResult:
    """Leg Gemini REST v1beta del tier. `temperature=None` no la envía."""
    model = get_model(tier)
    url = GEMINI_GENERATE_URL.format(model=model) + f"?key={_gemini_direct_key()}"
    contents, system_parts = _gemini_contents(messages)
    sampling: dict[str, Any] = {}
    if temperature is not None:
        sampling["temperature"] = temperature
    # GOTCHA L-014: gemini-2.5-pro RECHAZA thinking_budget=0 (400). Para el
    # tier gemini_pro va SIEMPRE thinking_budget=None (sin thinkingConfig).
    gen_cfg = gemini_config_rest(
        thinking_budget=None if tier == "gemini_pro" else 0,
        maxOutputTokens=max_tokens,
        **sampling,
    )
    if json_schema is not None:
        gen_cfg["responseMimeType"] = "application/json"
        gen_cfg["responseSchema"] = _gemini_schema(json_schema)
    body: dict[str, Any] = {"contents": contents, "generationConfig": gen_cfg}
    if system_parts:
        body["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system_parts)}]
        }

    status, raw = post(url, {"Content-Type": "application/json"}, body, timeout)
    if status != 200:
        raise _TransportFailure(f"HTTP {status}", status)
    payload = json.loads(raw)
    candidates = payload.get("candidates") or [{}]
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(
        str(p.get("text") or "") for p in parts if isinstance(p, dict)
    )
    meta = payload.get("usageMetadata") or {}
    usage: dict[str, int] = {}
    for src, dst in (
        ("promptTokenCount", "prompt_tokens"),
        ("candidatesTokenCount", "completion_tokens"),
        ("totalTokenCount", "total_tokens"),
    ):
        if src in meta:
            usage[dst] = int(meta[src])
    return LLMResult(
        text=text,
        parsed_json=_parse_json_text(text, json_schema),
        provider="google-direct",
        model=model,
        http_status=status,
        usage=usage,
    )


def _leg_openai(
    messages: list[Mapping[str, str]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float | None,
    post: HttpPost,
    timeout: float,
    *,
    model: str | None = None,
    provider_label: str = "openai-fallback",
    token_param: str = "max_tokens",
) -> LLMResult:
    """Leg OpenAI chat/completions.

    Sin `model` es el leg de fallback de la cascada legada (modelo de
    NK_MODEL_OPENAI_FALLBACK, etiqueta `openai-fallback`, `max_tokens`).
    complete_for lo llama con el modelo del tier, la etiqueta `openai-direct`
    y `token_param="max_completion_tokens"`: los modelos de razonamiento
    (gpt-5.x / gpt-6) rechazan `max_tokens` en la API directa. Con
    `temperature=None` no se envía (esos modelos tampoco la aceptan).
    """
    key = os.getenv(OPENAI_KEY_ENV, "").strip()
    if model is None:
        model = (
            os.getenv(OPENAI_FALLBACK_MODEL_ENV, "").strip()
            or OPENAI_FALLBACK_MODEL_DEFAULT
        )
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "messages": [dict(m) for m in messages],
        token_param: max_tokens,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if json_schema is not None:
        body["response_format"] = _openai_response_format(json_schema)

    status, raw = post(OPENAI_CHAT_URL, headers, body, timeout)
    if status != 200:
        raise _TransportFailure(f"HTTP {status}", status)
    payload = json.loads(raw)
    text = _openai_style_text(payload)
    return LLMResult(
        text=text,
        parsed_json=_parse_json_text(text, json_schema),
        provider=provider_label,
        model=model,
        http_status=status,
        usage=_token_usage(payload.get("usage") or {}),
    )


def _leg_anthropic(
    messages: list[Mapping[str, str]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float | None,
    post: HttpPost,
    timeout: float,
    *,
    model: str | None = None,
    provider_label: str = "anthropic-fallback",
    native_schema: bool = False,
) -> LLMResult:
    """Leg Anthropic Messages.

    Sin `model` es el leg de fallback de la cascada legada (modelo de
    NK_MODEL_ANTHROPIC_FALLBACK, etiqueta `anthropic-fallback`, schema por
    instrucción de sistema). complete_for lo llama con el modelo del tier, la
    etiqueta `anthropic-direct` y `native_schema=True`: structured outputs
    nativos (`output_config.format` json_schema), disponibles en la familia
    5.x. Con `temperature=None` no se envía: Opus 5.5 la rechaza siempre y
    Sonnet/Haiku 5.5 con cualquier valor distinto del default (400).
    """
    key = os.getenv(ANTHROPIC_KEY_ENV, "").strip()
    if model is None:
        model = (
            os.getenv(ANTHROPIC_FALLBACK_MODEL_ENV, "").strip()
            or ANTHROPIC_FALLBACK_MODEL_DEFAULT
        )
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    system_chunks = [
        str(m.get("content") or "") for m in messages if m.get("role") == "system"
    ]
    chat = [dict(m) for m in messages if m.get("role") != "system"]
    if json_schema is not None and not native_schema:
        # LÍMITE DOCUMENTADO del leg legado: schema por instrucción de sistema
        # + parseo del texto (_parse_json_text). Si el modelo no cumple,
        # parsed_json=None — misma semántica que un 200 con JSON inválido en
        # cualquier otro leg (no se relitiga).
        system_chunks.append(
            "Responde EXCLUSIVAMENTE con un objeto JSON válido que cumpla este "
            "JSON Schema, sin markdown ni texto adicional:\n"
            + json.dumps(json_schema, ensure_ascii=False)
        )
    body: dict[str, Any] = {
        "model": model,
        # max_tokens es OBLIGATORIO en la API Messages.
        "max_tokens": max_tokens,
        "messages": chat,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if json_schema is not None and native_schema:
        body["output_config"] = {
            "format": {"type": "json_schema", "schema": json_schema}
        }
    if system_chunks:
        body["system"] = "\n\n".join(system_chunks)

    status, raw = post(ANTHROPIC_MESSAGES_URL, headers, body, timeout)
    if status != 200:
        raise _TransportFailure(f"HTTP {status}", status)
    payload = json.loads(raw)
    text = "".join(
        str(block.get("text") or "")
        for block in (payload.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
    usage_raw = payload.get("usage") or {}
    usage: dict[str, int] = {}
    if "input_tokens" in usage_raw:
        usage["prompt_tokens"] = int(usage_raw["input_tokens"])
    if "output_tokens" in usage_raw:
        usage["completion_tokens"] = int(usage_raw["output_tokens"])
    if "prompt_tokens" in usage and "completion_tokens" in usage:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return LLMResult(
        text=text,
        parsed_json=_parse_json_text(text, json_schema),
        provider=provider_label,
        model=model,
        http_status=status,
        usage=usage,
    )


def _assert_model_family(tier: str, model: str) -> None:
    prefixes = _FAMILY_PREFIXES[tier]
    if model.startswith(prefixes):
        return
    raise CrossProviderOverrideError(
        f"NK_MODEL_{tier.upper()}={model!r} no pertenece a la familia {prefixes} "
        f"(guard L-014: no cruzar proveedores con un override de emergencia)"
    )


def _urllib_post(
    url: str, headers: Mapping[str, str], body: dict, timeout: float
) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")[:200]
