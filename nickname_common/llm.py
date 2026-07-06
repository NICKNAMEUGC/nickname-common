"""Registro central de modelos LLM del ecosistema Nickname (F0.1 Quality Leap 2026-06).

Fuente única de verdad para nombres de modelo y configs de generación.
Nace del incidente L-014: gemini-2.0-flash retirado por Google dejó 9
servicios/pipelines rotos en silencio durante semanas porque cada repo
hardcodeaba el string del modelo.

Con este registro:
  - Retirada de un modelo = actualizar _DEFAULTS aquí + bump del pin en los repos.
  - Override de EMERGENCIA por servicio sin tocar código: env var en Railway.
        NK_MODEL_GEMINI_FLASH=gemini-3-flash
  - El canary de NightWatch (task 15) vigila a diario que todos los modelos
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

Regla L-014 incorporada: thinking_budget=0 por defecto en los tiers flash —
en Gemini 2.5 el razonamiento consume max_output_tokens ANTES del output y
trunca JSON/respuestas con caps ajustados. Pasar thinking_budget=None para
permitir thinking dinámico (tiers de calidad, ej. redacción con gemini_pro).

GOTCHA (verificado 2026-06-12): gemini-2.5-pro RECHAZA thinking_budget=0 con
400 "Budget 0 is invalid. This model only works in thinking mode". Para el
tier gemini_pro usar SIEMPRE thinking_budget=None.
"""

import os

# Defaults vigentes (verificados 2026-07-06).
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
}

ENV_PREFIX = "NK_MODEL_"


def get_model(tier: str) -> str:
    """Modelo efectivo del tier: env var NK_MODEL_<TIER> > default del registro."""
    if tier not in _DEFAULTS:
        raise KeyError(
            f"Tier de modelo desconocido: {tier!r}. Válidos: {sorted(_DEFAULTS)}"
        )
    return os.getenv(ENV_PREFIX + tier.upper(), _DEFAULTS[tier])


def all_models() -> dict:
    """Mapa tier → modelo efectivo (overrides aplicados). Lo consume el canary."""
    return {tier: get_model(tier) for tier in _DEFAULTS}


def provider_of(tier: str) -> str:
    """Proveedor del tier: 'gemini' | 'anthropic'."""
    if tier not in _DEFAULTS:
        raise KeyError(f"Tier de modelo desconocido: {tier!r}")
    return "anthropic" if tier.startswith("claude") else "gemini"


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
