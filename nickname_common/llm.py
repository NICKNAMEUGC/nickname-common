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
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# Logger de módulo (stdlib): anomalías de config y fallos de leg de la cascada.
# NUNCA loguea contenido (prompts, respuestas, bodies) — solo proveedor,
# clase de error y HTTP status.
_LOG = logging.getLogger("nickname_common.llm")

# Defaults vigentes (verificados 2026-07-06; grok añadido 2026-08-23, dry).
# Histórico de retiradas: gemini-2.0-flash (Google, ~05/2026, L-014);
# imagen-4.0-generate-001 (Google, ~07/2026, L-037 — canary 15_model_canary,
# 404 en :predictImages, endpoint retirado por completo).
_DEFAULTS = {
    # Gemini
    "gemini_flash": "gemini-2.5-flash",       # clasificación, triaje, alto volumen
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
    # Anthropic
    "claude_sonnet": "claude-sonnet-4-20250514",  # judge IG agent, n8n Content Factory
    # xAI — opt-in. NUNCA alias de gemini_flash/gemini_pro (L-014).
    # No hay paridad de embeddings ni de generación de imagen.
    "grok_fast": "grok-3-mini",
    "grok_quality": "grok-4",
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
    "gemini_pro": "google/gemini-2.5-pro",
    "claude_sonnet": "anthropic/claude-sonnet-4",
    "grok_fast": "x-ai/grok-3-mini",
    "grok_quality": "x-ai/grok-4",
}

# Tiers que NO pueden pasar por el enrutador. No es pereza: OpenRouter es un
# API de chat. Ni embeddings ni generacion de imagen tienen paridad, y colarlos
# romperia el RAG de Nicky y el carrusel de LinkedIn en silencio.
_ROUTER_UNSUPPORTED = {"imagen"}

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
    "gemini_pro": ("gemini-",),
    "imagen": ("gemini-", "imagen-"),
    "claude_sonnet": ("claude-",),
    "grok_fast": ("grok-",),
    "grok_quality": ("grok-",),
}

_CAPABILITIES = {
    "gemini_flash": {
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
    "claude_sonnet": {
        "text": True,
        "json_schema": True,
        "embeddings": False,
        "images": False,
        "thinking_budget": False,
    },
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


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str
    model: str
    http_status: int
    parsed_json: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=dict)


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
    """Proveedor del tier: 'gemini' | 'anthropic' | 'xai'."""
    if tier not in _DEFAULTS:
        raise KeyError(f"Tier de modelo desconocido: {tier!r}")
    if tier.startswith("claude"):
        return "anthropic"
    if tier.startswith("grok"):
        return "xai"
    return "gemini"


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
    """


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

_GEMINI_DIRECT_TIERS = ("gemini_flash", "gemini_pro")


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


def _leg_openrouter(
    slug: str,
    leg: str,
    messages: list[Mapping[str, str]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float,
    post: HttpPost,
    timeout: float,
) -> LLMResult:
    key = os.getenv(ROUTER_KEY_ENV, "").strip()
    body: dict[str, Any] = {
        "model": slug,
        "messages": [dict(m) for m in messages],
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Sin esto OpenRouter devuelve tokens pero no coste, y el contador del
        # Command Center se quedaria otra vez sin la cifra que importa.
        "usage": {"include": True},
    }
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
        usage["cost_micros"] = int(round(float(usage_raw["cost"]) * 1_000_000))
    return LLMResult(
        text=text,
        parsed_json=_parse_json_text(text, json_schema),
        provider=leg,
        model=slug,
        http_status=status,
        usage=usage,
    )


def _gemini_schema(schema: Any) -> Any:
    """Limpia claves de JSON Schema que el responseSchema de Gemini rechaza."""
    if isinstance(schema, dict):
        return {
            k: _gemini_schema(v)
            for k, v in schema.items()
            if k not in _GEMINI_SCHEMA_DROP
        }
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
    temperature: float,
    post: HttpPost,
    timeout: float,
) -> LLMResult:
    model = get_model(tier)
    url = GEMINI_GENERATE_URL.format(model=model) + f"?key={_gemini_direct_key()}"
    contents, system_parts = _gemini_contents(messages)
    # GOTCHA L-014: gemini-2.5-pro RECHAZA thinking_budget=0 (400). Para el
    # tier gemini_pro va SIEMPRE thinking_budget=None (sin thinkingConfig).
    gen_cfg = gemini_config_rest(
        thinking_budget=None if tier == "gemini_pro" else 0,
        maxOutputTokens=max_tokens,
        temperature=temperature,
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
    temperature: float,
    post: HttpPost,
    timeout: float,
) -> LLMResult:
    key = os.getenv(OPENAI_KEY_ENV, "").strip()
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
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
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
        provider="openai-fallback",
        model=model,
        http_status=status,
        usage=_token_usage(payload.get("usage") or {}),
    )


def _leg_anthropic(
    messages: list[Mapping[str, str]],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    temperature: float,
    post: HttpPost,
    timeout: float,
) -> LLMResult:
    key = os.getenv(ANTHROPIC_KEY_ENV, "").strip()
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
    if json_schema is not None:
        # LÍMITE DOCUMENTADO: la API Messages de Anthropic no tiene
        # response_format json_schema. Aproximación pragmática: instrucción de
        # sistema con el schema + parseo del texto (_parse_json_text). Si el
        # modelo no cumple, parsed_json=None — misma semántica que un 200 con
        # JSON inválido en cualquier otro leg (no se relitiga).
        system_chunks.append(
            "Responde EXCLUSIVAMENTE con un objeto JSON válido que cumpla este "
            "JSON Schema, sin markdown ni texto adicional:\n"
            + json.dumps(json_schema, ensure_ascii=False)
        )
    body: dict[str, Any] = {
        "model": model,
        # max_tokens es OBLIGATORIO en la API Messages.
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": chat,
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
        provider="anthropic-fallback",
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
