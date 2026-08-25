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

Regla L-014 incorporada: thinking_budget=0 por defecto en los tiers flash —
en Gemini 2.5 el razonamiento consume max_output_tokens ANTES del output y
trunca JSON/respuestas con caps ajustados. Pasar thinking_budget=None para
permitir thinking dinámico (tiers de calidad, ej. redacción con gemini_pro).

GOTCHA (verificado 2026-06-12): gemini-2.5-pro RECHAZA thinking_budget=0 con
400 "Budget 0 is invalid. This model only works in thinking mode". Para el
tier gemini_pro usar SIEMPRE thinking_budget=None.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

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
    """Completa un chat. Fase 1: solo xAI. Gemini/Anthropic siguen en su SDK.

    Sin `http_post` inyectado requiere `XAI_API_KEY` y `NK_XAI_LIVE=1`.
    El default no llama a la red (Phase 1 dry / sin billing).
    """
    provider = provider_of(tier)
    enrutado = router_enabled()

    if enrutado:
        # El slug valida de paso que el tier sea enrutable (imagen/embeddings no).
        model = router_slug(tier)
        url = OPENROUTER_CHAT_URL
        key = os.getenv(ROUTER_KEY_ENV, "").strip()
        if http_post is None:
            if not key:
                raise RouterNotAvailable(
                    f"{ROUTER_ENV}=openrouter pero falta {ROUTER_KEY_ENV}. No se cae "
                    "al proveedor directo a proposito: enrutar a medias deja gasto "
                    "fuera del unico punto de control."
                )
            http_post = _urllib_post
        headers = _router_headers(key or "test")
    else:
        if provider != "xai":
            raise NotImplementedError(
                f"complete() sin enrutador solo implementa xAI; {provider} sigue en "
                f"su SDK. Para enrutarlo: {ROUTER_ENV}=openrouter"
            )
        model = get_model(tier)
        url = XAI_CHAT_URL
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
    if enrutado:
        # Sin esto OpenRouter devuelve tokens pero no coste, y el contador del
        # Command Center se quedaria otra vez sin la cifra que importa.
        body["usage"] = {"include": True}
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "result",
                "schema": json_schema,
                "strict": True,
            },
        }

    status, raw = http_post(url, headers, body, timeout)
    if status != 200:
        etiqueta = "OpenRouter" if enrutado else "xAI"
        raise ProviderNotConfigured(
            f"{etiqueta} chat/completions HTTP {status}: {raw[:200]}"
        )
    payload = json.loads(raw)
    text = (
        payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    parsed = None
    if json_schema is not None and text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    usage_raw = payload.get("usage") or {}
    usage = {
        k: int(usage_raw[k])
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        if k in usage_raw
    }
    if enrutado and usage_raw.get("cost") is not None:
        # Se guarda en milesimas de dolar para no meter floats en un dict[str, int].
        usage["cost_micros"] = int(round(float(usage_raw["cost"]) * 1_000_000))
    return LLMResult(
        text=text,
        parsed_json=parsed,
        provider=f"openrouter:{provider}" if enrutado else provider,
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
