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

Regla L-014 incorporada: razonamiento mínimo por defecto en los tiers flash —
el razonamiento consume max_output_tokens ANTES del output y trunca
JSON/respuestas con caps ajustados. Pasar thinking_budget=None para permitir
thinking dinámico (tiers de calidad, ej. redacción con gemini_pro).

GOTCHA (verificado 2026-06-12 en 2.5 y 2026-10-18 en 3.x): los Pro solo
funcionan en modo thinking (400 "Budget 0 is invalid"). Y Gemini 3.x no es
homogéneo (D-F, 2026-10-18): gemini-3.5-flash-lite rechaza thinkingBudget=0
y quiere thinkingLevel=minimal; gemini-3.8-flash acepta thinkingBudget=0 y
rechaza minimal; gemini-4-argon (gemini_pro desde el 2026-10-18, D-F bis)
acepta thinkingBudget=0 y rechaza minimal. `gemini_config_*(model=...)` y el leg directo lo resuelven
por modelo (`_GEMINI_MIN_THINKING`). En OpenRouter el razonamiento de Gemini
3.x es obligatorio (`reasoning.enabled=false` → 400): los tiers flash salen
con `reasoning.effort="low"` por defecto (0 tokens de razonamiento medidos).

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
no enrutable, como `imagen`). El guard L-014 cubre también el override de
slug `NK_MODEL_<TIER>_SLUG` (en `router_slug`, así que complete() lo hereda).
Este módulo importa `ai_policy` al final para validar la política al cargar.

Transporte, 2026-10-18 (D-F y entregables de la ola 1): Gemini 3.x en todos
los tiers Gemini (OpenRouter retira 2.5 el 2026-10-20); en complete_for los
pasos `@openrouter` se usan con solo tener `OPENROUTER_API_KEY` (rollback:
`NK_ROUTER=off`); `complete_for(..., tier=)` fija el tier; el paso OpenRouter
manda `provider.data_collection="deny"` y acepta `reasoning`;
`LLMResult.finish_reason` normalizado; mensajes multimodales estilo OpenAI,
`stop` y `strict` configurable en todos los legs. complete() legado conserva
su semántica de router (`NK_ROUTER=openrouter`).
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import logging
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from nickname_common.budget import BudgetExhausted, BudgetGuard
from nickname_common.cost_catalog import estimate_cost_micros, known_models
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
# D-F (Diego, 2026-10-18): OpenRouter retira google/gemini-2.5-flash,
# -flash-lite y -pro el 2026-10-20 (`expiration_date` en GET /api/v1/models,
# leído el 2026-10-18) y Google ya devuelve 404 «no longer available to new
# users» a gemini-2.5-flash-lite por la API directa. Todo Gemini pasa a 3.x
# en directo Y en OpenRouter; ids comprobados el 2026-10-18 en GET
# /v1beta/models de Google y en el listado público de OpenRouter. Cambiar el
# tier de un consumidor sigue exigiendo su eval (D-E); para la fase «solo
# transporte» complete_for acepta `tier=` y fija el tier sin cambiar de clase.
_DEFAULTS = {
    # Gemini
    "gemini_flash": "gemini-3.8-flash",       # clasificación, triaje, alto volumen
    "gemini_flash_lite": "gemini-3.5-flash-lite",  # jobs Railway de bajo coste (SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911): clasificación de un delta acotado, resumen pequeño de comentarios anonimizados. NUNCA redacción que deba superar un eval de calidad — para eso gemini_flash.
    # D-F bis (Diego, 2026-10-18): gemini_pro pasa a gemini-4-argon (GA, sin
    # expiration_date en OpenRouter), sustituyendo a gemini-3.1-pro-preview.
    "gemini_pro": "gemini-4-argon",           # redacción de calidad (drafts voice-cloned)
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
    "gemini_flash": "google/gemini-3.8-flash",
    "gemini_flash_lite": "google/gemini-3.5-flash-lite",
    "gemini_pro": "google/gemini-4-argon",
    "claude_sonnet": "anthropic/claude-sonnet-5.5",
    "claude_opus": "anthropic/claude-opus-5.5",
    "claude_haiku": "anthropic/claude-haiku-5.5",
    "gpt_quality": "openai/gpt-6-astra",
    "gpt_fast": "openai/gpt-5.4-mini",
    "grok_fast": "x-ai/grok-4.3",
    "grok_quality": "x-ai/grok-4.7",
}

# Prefijo de proveedor de los slugs de OpenRouter por familia (provider_of).
# El guard L-014 lo exige también al override NK_MODEL_<TIER>_SLUG.
_SLUG_PROVIDER_PREFIX = {
    "gemini": "google/",
    "anthropic": "anthropic/",
    "openai": "openai/",
    "xai": "x-ai/",
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
    # Motivo de fin normalizado en TODOS los legs (FINISH_REASONS): "stop",
    # "length", "content_filter" u "other". Campo nuevo con default: no
    # rompe a quien construye LLMResult a mano.
    finish_reason: str = "other"


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


# Razonamiento MÍNIMO que acepta cada modelo Gemini (regla L-014: proteger
# max_output_tokens). Verificado por API el 2026-10-18 con llamadas mínimas a
# :generateContent. Gemini 3.x NO es homogéneo, por eso va por modelo:
#   - gemini-3.5-flash-lite RECHAZA thinkingBudget=0 (400) y acepta
#     thinkingLevel=minimal (sin razonamiento).
#   - gemini-3.8-flash acepta thinkingBudget=0 (sin razonamiento) y RECHAZA
#     thinkingLevel=minimal (400).
#   - gemini-3.1-pro-preview y gemini-2.5-pro solo funcionan en modo thinking:
#     rechazan thinkingBudget=0 y thinkingLevel=minimal → sin thinkingConfig.
#   - gemini-4-argon (gemini_pro desde el 2026-10-18) acepta thinkingBudget=0
#     (0 thoughtsTokenCount), RECHAZA thinkingLevel=minimal (400) y acepta
#     low/high y budgets >0 o -1; sin thinkingConfig razona en dinámico.
#   - gemini-2.5-* no aceptan thinkingLevel («not supported for this model»).
# Un modelo fuera de la tabla (override NK_MODEL_*) usa la regla histórica:
# `pro` en el id → sin thinkingConfig; si no, thinkingBudget=0. Si un override
# nuevo la rechaza, el 400 lo verá el canary (task 15) o su smoke.
_GEMINI_MIN_THINKING: dict[str, dict[str, Any] | None] = {
    "gemini-2.5-flash": {"thinkingBudget": 0},
    "gemini-2.5-flash-lite": {"thinkingBudget": 0},
    "gemini-2.5-pro": None,
    "gemini-3.5-flash-lite": {"thinkingLevel": "minimal"},
    "gemini-3.8-flash": {"thinkingBudget": 0},
    "gemini-3.1-pro-preview": None,
    "gemini-4-argon": {"thinkingBudget": 0},
}

_SDK_THINKING_KEYS = {"thinkingBudget": "thinking_budget", "thinkingLevel": "thinking_level"}


def _gemini_min_thinking(model: str) -> dict[str, Any] | None:
    """thinkingConfig (REST) del razonamiento mínimo que acepta `model`."""
    if model in _GEMINI_MIN_THINKING:
        cfg = _GEMINI_MIN_THINKING[model]
        return dict(cfg) if cfg is not None else None
    if "pro" in model:
        return None
    return {"thinkingBudget": 0}


def _gemini_thinking(
    thinking_budget: int | None,
    *,
    model: str | None,
    tier: str | None,
    thinking_level: str | None,
) -> dict[str, Any] | None:
    """thinkingConfig REST (camelCase) o None si no hay que enviarlo."""
    if tier is not None and model is None:
        model = get_model(tier)
    if thinking_level is not None:
        if thinking_budget not in (0, None):
            raise ValueError(
                "thinking_budget y thinking_level son excluyentes: Gemini rechaza los dos"
            )
        return {"thinkingLevel": thinking_level}
    if thinking_budget is None:
        return None
    if thinking_budget == 0 and model is not None:
        return _gemini_min_thinking(model)
    return {"thinkingBudget": thinking_budget}


def gemini_config_sdk(
    thinking_budget=0,
    *,
    model: str | None = None,
    tier: str | None = None,
    thinking_level: str | None = None,
    **kwargs,
) -> dict:
    """Config para el SDK google-genai (snake_case; el SDK acepta dicts).

    thinking_budget=0 (default) = razonamiento mínimo / proteger
    max_output_tokens (L-014). Con `model=` (o `tier=`) se traduce a lo que
    acepta ESE modelo (Gemini 3.x no es homogéneo: ver `_GEMINI_MIN_THINKING`);
    sin modelo se envía `thinking_budget=0` como siempre, que vale para
    gemini_flash (gemini-3.8-flash) y gemini_pro (gemini-4-argon) pero NO para
    gemini_flash_lite (gemini-3.5-flash-lite): pásalo con `model=`/`tier=`.
    thinking_budget=None = thinking dinámico del modelo (tiers de calidad).
    thinking_level="low"/"medium"/"high"/"minimal" = nivel de Gemini 3.x
    (excluyente con un thinking_budget distinto de 0/None).
    """
    cfg = dict(kwargs)
    thinking = _gemini_thinking(
        thinking_budget, model=model, tier=tier, thinking_level=thinking_level
    )
    if thinking is not None:
        cfg["thinking_config"] = {_SDK_THINKING_KEYS[k]: v for k, v in thinking.items()}
    return cfg


def gemini_config_rest(
    thinking_budget=0,
    *,
    model: str | None = None,
    tier: str | None = None,
    thinking_level: str | None = None,
    **kwargs,
) -> dict:
    """generationConfig para la API REST v1beta (camelCase).

    Mismas semánticas de thinking_budget / model / tier / thinking_level que
    gemini_config_sdk().
    """
    cfg = dict(kwargs)
    thinking = _gemini_thinking(
        thinking_budget, model=model, tier=tier, thinking_level=thinking_level
    )
    if thinking is not None:
        cfg["thinkingConfig"] = thinking
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
    """¿Enruta complete() (LEGADO)? Fail-safe: sin la var, va directo al proveedor.

    complete_for NO usa esta función: ver `router_allowed_for_policy()`.
    """
    return os.getenv(ROUTER_ENV, "").strip().lower() == "openrouter"


ROUTER_OFF_VALUE = "off"


def router_allowed_for_policy() -> bool:
    """¿Puede complete_for usar los pasos `@openrouter`? (ola 1, 2026-10-18)

    En complete_for la política de la clase ya decide cuándo se usa
    OpenRouter, así que basta con tener `OPENROUTER_API_KEY`: no hace falta
    `NK_ROUTER=openrouter`. La palanca de rollback sin deploy es
    `NK_ROUTER=off`, que saca al servicio de OpenRouter (motivo
    `router-apagado`). complete() legado no cambia: sigue exigiendo
    `NK_ROUTER=openrouter` (`router_enabled()`).
    """
    return os.getenv(ROUTER_ENV, "").strip().lower() != ROUTER_OFF_VALUE


# Tiers Gemini cuyo razonamiento se reduce al mínimo por defecto también en
# OpenRouter (regla L-014, D-F): allí Gemini 3.x no admite apagarlo
# (`reasoning.enabled=false` → 400, comprobado el 2026-10-18) y `effort=low`
# dio 0 tokens de razonamiento en 3.5-flash-lite y 3.8-flash. gemini_pro
# conserva el razonamiento dinámico (calidad), como en el leg directo.
_ROUTER_DEFAULT_REASONING = {
    "gemini_flash": {"effort": "low"},
    "gemini_flash_lite": {"effort": "low"},
}


def _router_default_reasoning(tier: str) -> dict[str, Any] | None:
    cfg = _ROUTER_DEFAULT_REASONING.get(tier)
    return dict(cfg) if cfg is not None else None


def router_slug(tier: str) -> str:
    """Slug de OpenRouter para el tier. Conserva familia y modelo.

    El override `NK_MODEL_<TIER>_SLUG` pasa el mismo guard L-014 que
    `NK_MODEL_<TIER>`: un slug de otro proveedor o de otra familia lanza
    `CrossProviderOverrideError` (no cruzar de familia con un override).
    """
    if tier not in _DEFAULTS:
        raise KeyError(f"Tier de modelo desconocido: {tier!r}")
    if tier in _ROUTER_UNSUPPORTED:
        raise RouterNotAvailable(
            f"El tier {tier!r} no se enruta: OpenRouter es un API de chat y no hay "
            "paridad de embeddings ni de generacion de imagen. Sigue yendo directo."
        )
    slug = os.getenv(ENV_PREFIX + tier.upper() + "_SLUG", _ROUTER_SLUGS[tier])
    _assert_slug_family(tier, slug)
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
    byok_provider_order: bool = True,
) -> LLMResult:
    """Completa un chat.

    `byok_provider_order` (2026-10-18): en el leg OpenRouter pide primero el
    endpoint con la clave BYOK (google-ai-studio / anthropic) con
    `allow_fallbacks=True`. False no envía `provider.order`.

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
            byok_provider_order=byok_provider_order,
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
        "messages": _openai_messages(messages),
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
        finish_reason=_finish_openai_style(payload),
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
    byok_provider_order: bool = True,
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
                post, timeout, reasoning=_router_default_reasoning(tier),
                byok_provider_order=byok_provider_order,
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
    tier: str | None = None,
    stop: list[str] | None = None,
    json_schema_strict: bool = True,
    data_collection: str | None = "deny",
    reasoning: Mapping[str, Any] | None = None,
    byok_provider_order: bool = True,
) -> LLMResult:
    """Completa un chat según la política de la CLASE de trabajo.

    Parámetros añadidos el 2026-10-18 (todos opcionales, sin cambiar lo
    anterior):

    - `tier`: fija el tier (fase «solo transporte» de D-E: cambiar de familia
      solo con eval). Se recorren las MISMAS vías de la clase, en su orden,
      pero con ese tier (cada vía una vez y solo si sirve a su familia); si
      ninguna vía de la clase le sirve (o la clase no tiene cadena API), la
      cadena legado del tier: `openrouter` → directo de su familia. Si la
      clase tiene pasos de la familia del tier, manda el orden de vías de
      esos pasos (D2). Mismo presupuesto, mismo evento (`reason` lleva
      `;tier:<tier>`). Ver `ai_policy.api_chain_for_tier`.
    - `stop`: lista de stop sequences, en todos los legs (`stop` en
      OpenRouter/OpenAI, `stop_sequences` en Anthropic, `stopSequences` en
      Gemini).
    - `json_schema_strict`: `strict` del json_schema en los legs estilo
      OpenAI (OpenRouter, OpenAI); por defecto True, como hasta ahora.
    - `data_collection`: `provider.data_collection` del paso OpenRouter;
      "deny" por defecto (solo proveedores que no guardan ni entrenan con los
      datos). `None` no envía la preferencia.
    - `reasoning`: objeto `reasoning` de OpenRouter tal cual (`{"effort":
      "low"}`, `{"enabled": False}`, `{"max_tokens": n}`). Sin él, los tiers
      gemini flash salen con `{"effort": "low"}` (L-014). En Gemini directo se
      traduce a thinkingLevel/thinkingBudget; los pasos directos de Anthropic
      y OpenAI lo ignoran. Ojo: Gemini 3.x en OpenRouter rechaza
      `{"enabled": False}` (400, encadena).
    - `messages[i]["content"]` puede ser una lista de partes estilo OpenAI
      (text, image_url con data: URI o URL, file con PDF en base64): cada leg
      la traduce a su formato.
    - `byok_provider_order`: en el paso OpenRouter, `provider.order`
      `["google-ai-studio"]` para google/* y `["anthropic"]` para anthropic/*
      con `allow_fallbacks=True` (preferir el endpoint con clave BYOK sin
      perder Vertex u otros como respaldo). False no lo envía.

    La clase (`nickname_common.ai_policy`, fuente canónica) fija la cadena
    ordenada de pasos `{tier, via}`. Antes de tocar red o presupuesto se
    resuelve el modelo de TODOS los pasos: un override roto (`NK_MODEL_<TIER>`
    o `NK_MODEL_<TIER>_SLUG` que cruza de familia) es config, no transporte, y
    revienta sin encadenar. Después se recorre la cadena en orden:

    - Paso `@openrouter`: siempre que haya `OPENROUTER_API_KEY`, salvo
      `NK_ROUTER=off` (`router_allowed_for_policy()`), que es la palanca de
      rollback sin deploy (motivo `router-apagado`). A diferencia de
      complete() legado, NO hace falta `NK_ROUTER=openrouter`: la política de
      la clase ya decide cuándo se usa OpenRouter.
    - Pasos directos (`anthropic_direct`, `openai_direct`, `gemini_direct`):
      solo si existe su clave. `anthropic_direct` exige además la
      autorización D-A (`ai_policy.ANTHROPIC_DIRECT_AUTHORIZED`, por
      `NK_AGENT_NAME` y, en odoo-agent, por `job_id` `cfo...`): la clave sola
      no basta (motivo `no-autorizado-D-A`).
    - Con `guard`, un paso directo cuyo modelo no tiene precio verificado en
      `cost_catalog` se salta en cerrado (motivo `sin-precio`): Anthropic y
      OpenAI directos no informan coste, y sin precio el guard no podría
      sumar la llamada. Con `budget_exempt` la exención es explícita y el
      paso se ejecuta. Los pasos de OpenRouter traen el coste en la respuesta.
    - Solo los fallos de TRANSPORTE encadenan (HTTP >=400, red, timeout). Un
      200 con JSON de contenido inválido se devuelve (`parsed_json=None`,
      evento `content_error`) y NO se relitiga en el paso siguiente.
    - Fallo de PUERTA (red, timeout, HTTP 401 o 402: la clave o el host, no el
      modelo): el resto de pasos de esa misma vía se saltan (`puerta-caida`)
      en vez de repetir un intento que fallaría igual. Un 402 de OpenRouter
      (tope diario de la clave agotado) sigue encadenando, como fija el
      despliegue de flota del 2026-08-31, pero los pasos directos siguientes
      quedan acotados por el guard (precio verificado o `sin-precio`).
    - Todo agotado → `CascadeExhausted` con la cadena de intentos (vía, tier,
      motivo), nunca contenido; su evento es `timeout` si todos los intentos
      de red fueron timeouts y `transport_error` en cualquier otro caso.

    Presupuesto OBLIGATORIO: `guard` es un `BudgetGuard`; con `guard=None`
    hay que declarar `budget_exempt="<motivo>"` (si no, ValueError). Con
    guard: `check_before_call()` antes del primer paso (`BudgetExhausted` se
    propaga sin tocar la red, con `exc.usage_event` tipado como
    budget_blocked); tras el éxito, `record_call(coste)` y
    `check_call_cost(coste)`. Si el coste real supera el tope por llamada, la
    llamada YA ocurrió: se propaga `BudgetExhausted(PER_CALL_EXCEEDED)` con la
    respuesta en `exc.result` y el evento en `exc.usage_event`, para que el
    consumidor lo trate como anomalía sin perder lo pagado.

    Coste: el que informa OpenRouter (`usage.cost`; con BYOK, `is_byok=true`,
    ese campo es solo la comisión de OpenRouter y el proveedor factura aparte
    `cost_details.upstream_inference_cost`, así que el gasto es la SUMA; sin
    BYOK se toma `cost`) → si no hay, el catálogo verificado (`cost_catalog`,
    contando como salida los tokens de razonamiento de Gemini, que Google
    cobra a precio de salida) → si tampoco, `None` (`cost_source=unavailable`,
    nunca 0, y aviso en el log si había guard).

    El resultado lleva `usage_event` (LLMUsageEventV1, sin PII ni prompt;
    `output_tokens` son los facturados, razonamiento incluido).
    `LLMResult.provider` etiqueta el paso real: `openrouter:anthropic`,
    `openrouter:openai`, `openrouter:google`, `anthropic-direct`,
    `openai-direct` o `google-direct`.

    `temperature=None` (default) no envía el parámetro: la familia Claude 5.x y
    los GPT de razonamiento lo rechazan. `max_tokens` por defecto 1024: los
    modelos con razonamiento lo consumen antes del texto visible.
    """
    exempt = _validated_budget_args(guard, budget_exempt)
    stop = _stop_list(stop)
    if data_collection is not None and data_collection not in ("allow", "deny"):
        raise ValueError("data_collection debe ser 'deny', 'allow' o None")
    if reasoning is not None and not isinstance(reasoning, Mapping):
        raise TypeError("reasoning debe ser un dict con la forma de OpenRouter")
    steps = _api_steps_for(clase, tier)
    requested = [_step_requested_model(step) for step in steps]
    event_base = {
        "service": (
            service
            or os.getenv(ROUTER_AGENT_ENV, "").strip()
            or _UNIDENTIFIED_SERVICE
        ),
        "job_id": job_id or f"ai_policy:{clase}",
        "run_id": run_id or uuid.uuid4().hex,
        "reason": (reason or f"clase:{clase}")
        + (f";tier:{tier}" if tier else "")
        + (f";budget_exempt:{exempt}" if exempt else ""),
    }
    # Identidad del DESPLIEGUE para la autorización D-A: la variable de entorno
    # del servicio, no el kwarg `service` (que solo etiqueta el evento).
    agent = os.getenv(ROUTER_AGENT_ENV, "").strip()

    if guard is not None:
        try:
            guard.check_before_call()
        except BudgetExhausted as exc:
            exc.usage_event = LLMUsageEventV1.budget_blocked(
                requested_model=requested[0], **event_base
            )
            raise

    post = http_post if http_post is not None else _urllib_post
    intentos: list[str] = []
    fallos_de_red: list[Exception] = []
    puertas_caidas: set[str] = set()
    for step, model in zip(steps, requested):
        step_tier, via = step.tier, step.via
        if via == "openrouter":
            label = f"openrouter:{model.split('/', 1)[0]}"
        else:
            label = _DIRECT_VIA_LABELS[via]
        skip = _step_skip_reason(
            via, step_tier, model, label,
            guard=guard, agent=agent, job_id=event_base["job_id"],
            puertas_caidas=puertas_caidas,
        )
        if skip is not None:
            intentos.append(f"{label}[{step_tier}]({skip})")
            continue
        try:
            result = _run_step(
                via, step_tier, model, label, messages, json_schema, max_tokens,
                temperature, post, timeout,
                stop=stop, strict=json_schema_strict,
                data_collection=data_collection, reasoning=reasoning,
                byok_provider_order=byok_provider_order,
            )
        except _TRANSPORT_ERRORS as exc:
            motivo = (
                exc.reason if isinstance(exc, _TransportFailure) else type(exc).__name__
            )
            intentos.append(f"{label}[{step_tier}]({motivo})")
            fallos_de_red.append(exc)
            if _is_door_failure(exc):
                puertas_caidas.add(via)
            _LOG.warning(
                "complete_for clase=%s: paso %s[%s] falló (%s); se intenta el siguiente",
                clase, label, step_tier, motivo,
            )
            continue
        return _settle_result(
            result, guard=guard, event_base=event_base, json_schema=json_schema
        )

    exhausted = CascadeExhausted(
        f"Cadena API agotada para la clase {clase!r}. "
        "Intentos (vía[tier](motivo)): " + " -> ".join(intentos)
    )
    todo_timeout = bool(fallos_de_red) and all(_is_timeout(e) for e in fallos_de_red)
    exhausted.usage_event = LLMUsageEventV1(
        provider="none",
        requested_model=requested[0],
        outcome=LLMOutcome.TIMEOUT if todo_timeout else LLMOutcome.TRANSPORT_ERROR,
        cost_source=CostSource.UNAVAILABLE,
        **event_base,
    )
    raise exhausted


def _step_skip_reason(
    via: str,
    tier: str,
    model: str,
    label: str,
    *,
    guard: BudgetGuard | None,
    agent: str,
    job_id: str,
    puertas_caidas: set[str],
) -> str | None:
    """Motivo para saltar el paso sin tocar la red, o None si se intenta."""
    if via in puertas_caidas:
        return "puerta-caida"
    if via == "openrouter":
        if not router_allowed_for_policy():
            return "router-apagado"
        if not os.getenv(ROUTER_KEY_ENV, "").strip():
            if router_enabled():
                _LOG.warning(
                    "%s=openrouter pero falta %s: anomalía de config; complete_for "
                    "salta el paso %s[%s]", ROUTER_ENV, ROUTER_KEY_ENV, label, tier,
                )
            return "sin-credencial"
        return None
    if via == "anthropic_direct":
        if not os.getenv(ANTHROPIC_KEY_ENV, "").strip():
            return "sin-credencial"
        if not _ai_policy.anthropic_direct_allowed(agent, job_id):
            return "no-autorizado-D-A"
    elif via == "openai_direct":
        if not os.getenv(OPENAI_KEY_ENV, "").strip():
            return "sin-credencial"
    elif via == "gemini_direct":
        if not _gemini_direct_key():
            return "sin-credencial"
    else:  # pragma: no cover — ai_policy valida las vías al importarse
        raise ValueError(f"Vía desconocida en la política: {via!r}")
    # Fail-closed (budget.py: «ausencia de certeza de gasto bloquea»): un paso
    # directo no trae coste en la respuesta; sin precio verificado el guard no
    # podría sumarlo y quedaría inerte justo cuando OpenRouter ha caído.
    if guard is not None and model not in known_models():
        return "sin-precio"
    return None


def _run_step(
    via: str,
    tier: str,
    model: str,
    label: str,
    messages: list[Mapping[str, str]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float | None,
    post: HttpPost,
    timeout: float,
    *,
    stop: list[str] | None = None,
    strict: bool = True,
    data_collection: str | None = None,
    reasoning: Mapping[str, Any] | None = None,
    byok_provider_order: bool = True,
) -> LLMResult:
    """Ejecuta un paso de la cadena con el leg de su vía."""
    if via == "openrouter":
        return _leg_openrouter(
            model, label, messages, json_schema, max_tokens, temperature,
            post, timeout, track_upstream_cost=True, stop=stop, strict=strict,
            data_collection=data_collection,
            reasoning=reasoning if reasoning is not None else _router_default_reasoning(tier),
            byok_provider_order=byok_provider_order,
        )
    if via == "anthropic_direct":
        return _leg_anthropic(
            messages, json_schema, max_tokens, temperature, post, timeout,
            model=model, provider_label=label, native_schema=True, stop=stop,
        )
    if via == "openai_direct":
        return _leg_openai(
            messages, json_schema, max_tokens, temperature, post, timeout,
            model=model, provider_label=label,
            token_param="max_completion_tokens", stop=stop, strict=strict,
        )
    if via == "gemini_direct":
        return _leg_gemini_direct(
            tier, messages, json_schema, max_tokens, temperature, post, timeout,
            track_reasoning_tokens=True, stop=stop, reasoning=reasoning,
        )
    raise ValueError(f"Vía desconocida en la política: {via!r}")  # pragma: no cover


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(
        getattr(exc, "reason", None), TimeoutError
    )


def _is_door_failure(exc: BaseException) -> bool:
    """¿Falla la PUERTA (clave o host) y no el modelo? Red, timeout, 401, 402."""
    if isinstance(exc, _TransportFailure):
        return exc.http_status in (401, 402)
    return isinstance(exc, OSError)


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


def _api_steps_for(clase: str, tier: str | None = None) -> tuple:
    """Cadena API ejecutable de la clase (o con el tier fijado), o ClassNotApiExecutable."""
    if tier is not None:
        if tier not in _DEFAULTS:
            raise KeyError(f"Tier de modelo desconocido: {tier!r}")
        if not capabilities(tier)["text"]:
            raise ClassNotApiExecutable(
                f"El tier {tier!r} no es de chat: se llama por su endpoint propio "
                "con get_model(tier), no por complete_for"
            )
        return _ai_policy.api_chain_for_tier(clase, tier)
    steps = _ai_policy.api_chain(clase)
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


def _billed_output_tokens(usage: Mapping[str, int]) -> int | None:
    """Tokens de salida FACTURADOS: texto + razonamiento.

    OpenRouter, OpenAI y Anthropic ya incluyen el razonamiento en su recuento
    de salida; Gemini directo lo informa aparte (`thoughtsTokenCount`, que el
    leg guarda como `reasoning_tokens` solo para complete_for) y Google lo
    cobra a precio de salida. Sin ninguno de los dos recuentos: None.
    """
    completion = usage.get("completion_tokens")
    reasoning = usage.get("reasoning_tokens")
    if completion is None and reasoning is None:
        return None
    return (completion or 0) + (reasoning or 0)


def _result_cost(result: LLMResult) -> tuple[int | None, CostSource]:
    usage = result.usage
    cost = usage.get("cost_micros")
    upstream = usage.get("upstream_cost_micros")
    if cost is not None or upstream is not None:
        byok = usage.get("is_byok")
        if byok == 1 and upstream is not None:
            # BYOK: `cost` es la comisión de OpenRouter (en sus créditos) y
            # upstream lo que factura el proveedor (crédito Team en D-A): el
            # gasto real es la suma de los dos.
            total = upstream if cost is None else cost + upstream
        elif byok == 0:
            # Sin BYOK, upstream repite el mismo importe (o viene a 0/null):
            # sumarlo contaría dos veces. Recibo: docs/evidence/flash-lite-cost-20260913.json.
            total = cost if cost is not None else upstream
        else:
            if byok == 1:
                _LOG.warning(
                    "OpenRouter marcó BYOK sin cost_details.upstream_inference_cost: "
                    "solo se conoce la comisión; el gasto del proveedor queda sin sumar"
                )
            # Sin la marca is_byok no se sabe si upstream repite el importe: el
            # mayor de los dos nunca cuenta dos veces.
            total = max(v for v in (cost, upstream) if v is not None)
        return total, CostSource.PROVIDER_RESPONSE
    # Catálogo: ids sin prefijo de OpenRouter (precio pass-through idéntico).
    estimated = estimate_cost_micros(
        result.model.split("/", 1)[-1],
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=_billed_output_tokens(usage),
    )
    if estimated is not None:
        return estimated, CostSource.CATALOG
    return None, CostSource.UNAVAILABLE


def _settle_result(
    result: LLMResult,
    *,
    guard: BudgetGuard | None,
    event_base: dict[str, str],
    json_schema: dict[str, Any] | None,
) -> LLMResult:
    cost, source = _result_cost(result)
    if cost is None and guard is not None:
        _LOG.warning(
            "complete_for: %s sirvió %s sin coste en la respuesta ni precio de "
            "catálogo; el BudgetGuard no puede sumar esta llamada (anomalía)",
            result.provider, result.model,
        )
    # Un 200 que no da el JSON pedido (inválido o cortado por max_tokens) se
    # pagó pero no sirve al consumidor: content_error, sin encadenar.
    outcome = (
        LLMOutcome.CONTENT_ERROR
        if json_schema is not None and result.parsed_json is None
        else LLMOutcome.SERVED
    )
    event = LLMUsageEventV1(
        provider=result.provider,
        requested_model=result.model,
        outcome=outcome,
        input_tokens=result.usage.get("prompt_tokens"),
        output_tokens=_billed_output_tokens(result.usage),
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


def _openai_response_format(
    json_schema: dict[str, Any], strict: bool = True
) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "result",
            "schema": json_schema,
            "strict": strict,
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


# --- Preferencia de proveedor BYOK en OpenRouter (2026-10-18) ---------------
# Las claves BYOK de Diego viven en Google AI Studio y en Anthropic: se pide a
# OpenRouter que pruebe primero ese endpoint (`provider.order`) sin perder el
# resto como respaldo (`allow_fallbacks=True`, p.ej. Vertex). Comprobado con
# llamadas reales el 2026-10-18 (GET /api/v1/generation: provider_name
# "Google AI Studio", is_byok=true). Se desactiva con
# `byok_provider_order=False` en complete() y complete_for().
_BYOK_PROVIDER_ORDER = {
    "google": ("google-ai-studio",),
    "anthropic": ("anthropic",),
}


def _router_provider_prefs(
    slug: str, *, data_collection: str | None, byok_provider_order: bool
) -> dict[str, Any] | None:
    """Objeto `provider` del body de OpenRouter, o None si no hay nada que pedir."""
    prefs: dict[str, Any] = {}
    order = _BYOK_PROVIDER_ORDER.get(slug.split("/", 1)[0]) if byok_provider_order else None
    if order:
        prefs["order"] = list(order)
        prefs["allow_fallbacks"] = True
    if data_collection is not None:
        prefs["data_collection"] = data_collection
    return prefs or None


# --- finish_reason normalizado ------------------------------------------------
# LLMResult.finish_reason vale siempre uno de FINISH_REASONS, sea cual sea el
# leg: `stop` (terminó o tocó una stop sequence), `length` (max_tokens o
# ventana de contexto), `content_filter` (filtro de seguridad, rechazo o
# bloqueo del prompt) u `other` (herramientas, error, ausente...).
FINISH_REASONS = ("stop", "length", "content_filter", "other")

_GEMINI_FINISH = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
}

_ANTHROPIC_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
}


def _finish_openai_style(payload: Mapping[str, Any]) -> str:
    """OpenAI, OpenRouter (ya normaliza) y xAI: choices[0].finish_reason."""
    choices = payload.get("choices") or [{}]
    raw = (choices[0] or {}).get("finish_reason") if isinstance(choices[0], dict) else None
    return raw if raw in ("stop", "length", "content_filter") else "other"


def _finish_gemini(payload: Mapping[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback") or {}
        return "content_filter" if feedback.get("blockReason") else "other"
    return _GEMINI_FINISH.get(str(candidates[0].get("finishReason") or ""), "other")


def _finish_anthropic(payload: Mapping[str, Any]) -> str:
    return _ANTHROPIC_FINISH.get(str(payload.get("stop_reason") or ""), "other")


# --- Mensajes multimodales ----------------------------------------------------
# `content` puede ser un str (como siempre) o una LISTA de partes estilo OpenAI:
#   {"type": "text", "text": "..."}
#   {"type": "image_url", "image_url": {"url": "data:image/png;base64,..." | "https://..."}}
#   {"type": "file", "file": {"filename": "x.pdf", "file_data": "data:application/pdf;base64,..."}}
# OpenRouter y OpenAI (chat/completions) las reciben tal cual; Anthropic las
# recibe como bloques image/document; Gemini como inlineData/fileData. Un
# mensaje de sistema solo admite texto. Una parte desconocida es un error de
# programación (ValueError), no de transporte: no encadena.


def _data_uri(uri: str) -> tuple[str, str] | None:
    """`data:<mime>;base64,<datos>` → (mime, datos). None si no es data: URI."""
    if not uri.startswith("data:"):
        return None
    header, sep, data = uri[5:].partition(",")
    if not sep or ";base64" not in header:
        raise ValueError(
            "parte multimodal: la data: URI debe ir en base64 (data:<mime>;base64,<datos>)"
        )
    return (header.split(";", 1)[0] or "application/octet-stream"), data


def _normalized_parts(content: Any) -> list[dict[str, str]]:
    """content (str o lista de partes estilo OpenAI) → partes neutras."""
    if not isinstance(content, list):
        return [{"kind": "text", "text": str(content or "")}]
    parts: list[dict[str, str]] = []
    for part in content:
        if not isinstance(part, Mapping):
            raise ValueError(f"parte multimodal inválida: {type(part).__name__}")
        ptype = part.get("type")
        if ptype == "text":
            parts.append({"kind": "text", "text": str(part.get("text") or "")})
        elif ptype == "image_url":
            ref = part.get("image_url")
            url = ref.get("url") if isinstance(ref, Mapping) else ref
            if not isinstance(url, str) or not url:
                raise ValueError("parte image_url sin url")
            data = _data_uri(url)
            if data is not None:
                parts.append({"kind": "image", "mime": data[0], "data": data[1]})
            else:
                parts.append({"kind": "image", "url": url})
        elif ptype == "file":
            ref = part.get("file")
            file_data = ref.get("file_data") if isinstance(ref, Mapping) else None
            if not isinstance(file_data, str) or not file_data:
                raise ValueError("parte file sin file_data (PDF en base64)")
            data = _data_uri(file_data) or ("application/pdf", file_data)
            parts.append({
                "kind": "file",
                "mime": data[0],
                "data": data[1],
                "filename": str(ref.get("filename") or "documento.pdf"),
            })
        else:
            raise ValueError(
                f"tipo de parte multimodal no soportado: {ptype!r} (text, image_url, file)"
            )
    return parts


def _text_only(content: Any) -> str:
    """Texto de un mensaje de sistema; las partes no textuales no caben ahí."""
    parts = _normalized_parts(content)
    if any(p["kind"] != "text" for p in parts):
        raise ValueError("un mensaje de sistema solo admite partes de texto")
    return "".join(p["text"] for p in parts)


def _openai_messages(messages: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Mensajes para OpenRouter / OpenAI / xAI: las partes viajan tal cual.

    Solo se valida la lista y se completa un `file_data` en base64 sin
    prefijo con su data: URI (las dos APIs la exigen).
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        msg = dict(m)
        content = msg.get("content")
        if isinstance(content, list):
            parts = _normalized_parts(content)
            rebuilt: list[Any] = []
            for original, part in zip(content, parts):
                if part["kind"] == "file":
                    rebuilt.append({
                        "type": "file",
                        "file": {
                            "filename": part["filename"],
                            "file_data": f"data:{part['mime']};base64,{part['data']}",
                        },
                    })
                else:
                    rebuilt.append(dict(original))
            msg["content"] = rebuilt
        out.append(msg)
    return out


def _anthropic_content(content: Any) -> Any:
    """Partes estilo OpenAI → bloques de la API Messages de Anthropic."""
    if not isinstance(content, list):
        return content
    blocks: list[dict[str, Any]] = []
    for part in _normalized_parts(content):
        if part["kind"] == "text":
            blocks.append({"type": "text", "text": part["text"]})
        elif part["kind"] == "image" and "url" in part:
            blocks.append({"type": "image", "source": {"type": "url", "url": part["url"]}})
        elif part["kind"] == "image":
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": part["mime"], "data": part["data"]},
            })
        else:
            if part["mime"] != "application/pdf":
                raise ValueError(
                    f"Anthropic solo acepta documentos PDF en base64, no {part['mime']!r}"
                )
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": part["data"]},
            })
    return blocks


def _gemini_parts(content: Any) -> list[dict[str, Any]]:
    """Partes estilo OpenAI → parts de Gemini REST (inlineData / fileData)."""
    parts: list[dict[str, Any]] = []
    for part in _normalized_parts(content):
        if part["kind"] == "text":
            parts.append({"text": part["text"]})
        elif "url" in part:
            mime = mimetypes.guess_type(part["url"])[0] or "image/jpeg"
            parts.append({"fileData": {"mimeType": mime, "fileUri": part["url"]}})
        else:
            parts.append({"inlineData": {"mimeType": part["mime"], "data": part["data"]}})
    return parts


def _stop_list(stop: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if stop is None:
        return None
    if isinstance(stop, str) or not all(isinstance(s, str) and s for s in stop):
        raise ValueError("stop debe ser una lista de cadenas no vacías")
    return list(stop) or None


def _leg_openrouter(
    slug: str,
    leg: str,
    messages: list[Mapping[str, Any]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float | None,
    post: HttpPost,
    timeout: float,
    *,
    track_upstream_cost: bool = False,
    stop: list[str] | None = None,
    strict: bool = True,
    data_collection: str | None = None,
    reasoning: Mapping[str, Any] | None = None,
    byok_provider_order: bool = True,
) -> LLMResult:
    """Leg OpenRouter. `temperature=None` no la envía (solo complete_for).

    `track_upstream_cost=True` (solo complete_for) añade a `usage` la clave
    `upstream_cost_micros` cuando OpenRouter informa
    `usage.cost_details.upstream_inference_cost`, y `is_byok` (1/0) cuando
    informa `usage.is_byok`: con BYOK (decisión D-A, el crédito Team de
    Anthropic) `cost` es solo la comisión de OpenRouter y el gasto del modelo
    viaja en upstream. complete() no lo pide; para él, con BYOK, `cost_micros`
    pasa a ser comisión + upstream (el gasto real), no solo la comisión.

    `data_collection` → `provider.data_collection` (complete_for manda
    "deny": solo proveedores que no guardan ni entrenan con los datos).
    `reasoning` → el objeto `reasoning` de OpenRouter tal cual
    (`{"effort": "low"}`, `{"enabled": False}`, `{"max_tokens": n}`).
    """
    key = os.getenv(ROUTER_KEY_ENV, "").strip()
    body: dict[str, Any] = {
        "model": slug,
        "messages": _openai_messages(messages),
        "max_tokens": max_tokens,
        # Sin esto OpenRouter devuelve tokens pero no coste, y el contador del
        # Command Center se quedaria otra vez sin la cifra que importa.
        "usage": {"include": True},
    }
    if temperature is not None:
        body["temperature"] = temperature
    if json_schema is not None:
        body["response_format"] = _openai_response_format(json_schema, strict)
    if stop:
        body["stop"] = list(stop)
    provider_prefs = _router_provider_prefs(
        slug, data_collection=data_collection, byok_provider_order=byok_provider_order
    )
    if provider_prefs is not None:
        body["provider"] = provider_prefs
    if reasoning is not None:
        body["reasoning"] = dict(reasoning)

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
    details = usage_raw.get("cost_details") or {}
    upstream = details.get("upstream_inference_cost") if isinstance(details, dict) else None
    if track_upstream_cost:
        if upstream is not None:
            usage["upstream_cost_micros"] = _usd_to_micros(upstream)
        if isinstance(usage_raw.get("is_byok"), bool):
            usage["is_byok"] = int(usage_raw["is_byok"])
    elif usage_raw.get("is_byok") is True and upstream is not None:
        # complete() legado: con BYOK `cost` es solo la comisión de OpenRouter
        # (0 en las llamadas reales del 2026-10-18) y el gasto del modelo viaja
        # en upstream. Sus consumidores suman `cost_micros` a su BudgetGuard:
        # sin esto registrarían 0 por llamadas que Google/Anthropic sí cobran.
        usage["cost_micros"] = usage.get("cost_micros", 0) + _usd_to_micros(upstream)
    return LLMResult(
        text=text,
        parsed_json=_parse_json_text(text, json_schema),
        provider=leg,
        model=slug,
        http_status=status,
        usage=usage,
        finish_reason=_finish_openai_style(payload),
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
    messages: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Convierte messages estilo chat a contents de Gemini (assistant→model)."""
    contents: list[dict[str, Any]] = []
    system_parts: list[str] = []
    for m in messages:
        role = str(m.get("role") or "user")
        content = m.get("content")
        if role == "system":
            system_parts.append(_text_only(content))
            continue
        contents.append({
            "role": "model" if role == "assistant" else "user",
            "parts": _gemini_parts(content),
        })
    return contents, system_parts


def _gemini_leg_thinking(
    tier: str, model: str, reasoning: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """thinkingConfig del leg directo; `reasoning` usa la forma de OpenRouter.

    Sin `reasoning`: razonamiento mínimo del modelo en los tiers flash y
    dinámico en gemini_pro (calidad), como siempre (GOTCHA L-014).
    `{"effort": X}` → thinkingLevel X (Gemini 3.x y posteriores; en 2.x se ignora);
    `{"max_tokens": n}` → thinkingBudget n; `{"enabled": False}` → mínimo;
    `{"enabled": True}` → dinámico.
    """
    budget: int | None = None if tier == "gemini_pro" else 0
    level: str | None = None
    if reasoning:
        # thinkingLevel existe desde Gemini 3 (incluido gemini-4-argon); los
        # 2.x lo rechazan («not supported for this model»).
        if reasoning.get("effort") is not None and not model.startswith(("gemini-2", "gemini-1")):
            effort = str(reasoning["effort"])
            level = "high" if effort == "xhigh" else effort
            budget = None
        elif reasoning.get("max_tokens") is not None:
            budget = int(reasoning["max_tokens"])
        elif reasoning.get("enabled") is False:
            budget = 0
        elif reasoning.get("enabled") is True:
            budget = None
    return _gemini_thinking(budget, model=model, tier=None, thinking_level=level)


def _leg_gemini_direct(
    tier: str,
    messages: list[Mapping[str, Any]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float | None,
    post: HttpPost,
    timeout: float,
    *,
    track_reasoning_tokens: bool = False,
    stop: list[str] | None = None,
    reasoning: Mapping[str, Any] | None = None,
) -> LLMResult:
    """Leg Gemini REST v1beta del tier. `temperature=None` no la envía.

    `track_reasoning_tokens=True` (solo complete_for) añade a `usage` la clave
    `reasoning_tokens` con `usageMetadata.thoughtsTokenCount`: Google cobra
    el razonamiento a precio de salida y `candidatesTokenCount` no lo
    incluye. complete() no lo pide, así que su `usage` no cambia.
    """
    model = get_model(tier)
    url = GEMINI_GENERATE_URL.format(model=model) + f"?key={_gemini_direct_key()}"
    contents, system_parts = _gemini_contents(messages)
    gen_cfg: dict[str, Any] = {"maxOutputTokens": max_tokens}
    if temperature is not None:
        gen_cfg["temperature"] = temperature
    # GOTCHA L-014 + D-F: el razonamiento mínimo depende del MODELO (los Pro
    # solo funcionan en modo thinking; 3.5-flash-lite quiere thinkingLevel y
    # 3.8-flash thinkingBudget). Ver _GEMINI_MIN_THINKING.
    thinking = _gemini_leg_thinking(tier, model, reasoning)
    if thinking is not None:
        gen_cfg["thinkingConfig"] = thinking
    if stop:
        gen_cfg["stopSequences"] = list(stop)
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
        str(p.get("text") or "")
        for p in parts
        if isinstance(p, dict) and not p.get("thought")
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
    if track_reasoning_tokens and "thoughtsTokenCount" in meta:
        usage["reasoning_tokens"] = int(meta["thoughtsTokenCount"])
    return LLMResult(
        text=text,
        parsed_json=_parse_json_text(text, json_schema),
        provider="google-direct",
        model=model,
        http_status=status,
        usage=usage,
        finish_reason=_finish_gemini(payload),
    )


def _leg_openai(
    messages: list[Mapping[str, Any]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float | None,
    post: HttpPost,
    timeout: float,
    *,
    model: str | None = None,
    provider_label: str = "openai-fallback",
    token_param: str = "max_tokens",
    stop: list[str] | None = None,
    strict: bool = True,
) -> LLMResult:
    """Leg OpenAI chat/completions.

    Sin `model` es el leg de fallback de la cascada legada (modelo de
    NK_MODEL_OPENAI_FALLBACK, etiqueta `openai-fallback`, `max_tokens`).
    complete_for lo llama con el modelo del tier, la etiqueta `openai-direct`
    y `token_param="max_completion_tokens"`: los modelos de razonamiento
    (gpt-5.x / gpt-6) rechazan `max_tokens` en la API directa. Con
    `temperature=None` no se envía (esos modelos tampoco la aceptan).
    Partes multimodales: chat/completions acepta `image_url` y `file` tal
    cual. `stop` va como `stop` (ojo: algunos modelos de razonamiento lo
    rechazan con 400, que encadena como cualquier fallo de transporte).
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
        "messages": _openai_messages(messages),
        token_param: max_tokens,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if json_schema is not None:
        body["response_format"] = _openai_response_format(json_schema, strict)
    if stop:
        body["stop"] = list(stop)

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
        finish_reason=_finish_openai_style(payload),
    )


def _leg_anthropic(
    messages: list[Mapping[str, Any]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float | None,
    post: HttpPost,
    timeout: float,
    *,
    model: str | None = None,
    provider_label: str = "anthropic-fallback",
    native_schema: bool = False,
    stop: list[str] | None = None,
) -> LLMResult:
    """Leg Anthropic Messages.

    Sin `model` es el leg de fallback de la cascada legada (modelo de
    NK_MODEL_ANTHROPIC_FALLBACK, etiqueta `anthropic-fallback`, schema por
    instrucción de sistema). complete_for lo llama con el modelo del tier, la
    etiqueta `anthropic-direct` y `native_schema=True`: structured outputs
    nativos (`output_config.format` json_schema), disponibles en la familia
    5.x. Con `temperature=None` no se envía: Opus 5.5 la rechaza siempre y
    Sonnet/Haiku 5.5 con cualquier valor distinto del default (400).
    Partes multimodales → bloques `image` (base64 o url) y `document` (PDF);
    `stop` → `stop_sequences`.
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
        _text_only(m.get("content")) for m in messages if m.get("role") == "system"
    ]
    chat = []
    for m in messages:
        if m.get("role") == "system":
            continue
        msg = dict(m)
        msg["content"] = _anthropic_content(msg.get("content"))
        chat.append(msg)
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
    if stop:
        body["stop_sequences"] = list(stop)
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
        finish_reason=_finish_anthropic(payload),
    )


def _assert_model_family(tier: str, model: str) -> None:
    prefixes = _FAMILY_PREFIXES[tier]
    if model.startswith(prefixes):
        return
    raise CrossProviderOverrideError(
        f"NK_MODEL_{tier.upper()}={model!r} no pertenece a la familia {prefixes} "
        f"(guard L-014: no cruzar proveedores con un override de emergencia)"
    )


def _assert_slug_family(tier: str, slug: str) -> None:
    provider_prefix = _SLUG_PROVIDER_PREFIX[provider_of(tier)]
    prefixes = _FAMILY_PREFIXES[tier]
    if slug.startswith(provider_prefix) and slug[len(provider_prefix):].startswith(prefixes):
        return
    raise CrossProviderOverrideError(
        f"NK_MODEL_{tier.upper()}_SLUG={slug!r} no pertenece a la familia "
        f"{provider_prefix}{{{','.join(prefixes)}}}* (guard L-014: el slug de "
        "OpenRouter tampoco cruza proveedores con un override de emergencia)"
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


# Al final a propósito: ai_policy importa este módulo para validar sus tiers.
# Importarla aquí valida la política al cargar `nickname_common.llm`: una
# política incoherente rompe el arranque del servicio, no la primera llamada
# a complete_for (SPEC-AI-PLANE-20261017 §5.4).
from nickname_common import ai_policy as _ai_policy  # noqa: E402
