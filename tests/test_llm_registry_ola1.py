"""Registro de modelos tras la ola 1 del plano IA (2026-10-17).

Dos cosas distintas, cada una con sus tests:
  1. REPARACIÓN: claude-sonnet-4-20250514 / anthropic/claude-sonnet-4 y
     x-ai/grok-3-mini / x-ai/grok-4 ya no existen (verificado por API el
     2026-10-17). No es un cambio de calidad: el modelo había desaparecido.
  2. TIERS NUEVOS: claude_opus, claude_haiku, gpt_quality, gpt_fast y
     gemini_embedding (no enrutable), con su slug, su familia (guard L-014) y
     sus capacidades.
"""

import pytest

import nickname_common
from nickname_common import llm
from nickname_common.llm import CrossProviderOverrideError, RouterNotAvailable

_RETIRADOS = {
    "gemini-2.0-flash",
    "imagen-4.0-generate-001",
    "claude-sonnet-4-20250514",
    "grok-3-mini",
    "grok-4",
}
_SLUGS_RETIRADOS = {
    "anthropic/claude-sonnet-4",
    "x-ai/grok-3-mini",
    "x-ai/grok-4",
}

_NUEVOS = {
    # tier: (modelo directo, slug OpenRouter, proveedor)
    "claude_opus": ("claude-opus-5-5", "anthropic/claude-opus-5.5", "anthropic"),
    "claude_haiku": ("claude-haiku-5-5", "anthropic/claude-haiku-5.5", "anthropic"),
    "gpt_quality": ("gpt-6-astra", "openai/gpt-6-astra", "openai"),
    "gpt_fast": ("gpt-5.4-mini", "openai/gpt-5.4-mini", "openai"),
}

_REPARADOS = {
    "claude_sonnet": ("claude-sonnet-5-5", "anthropic/claude-sonnet-5.5"),
    "grok_fast": ("grok-4.3", "x-ai/grok-4.3"),
    "grok_quality": ("grok-4.7", "x-ai/grok-4.7"),
}

_PREFIJO_SLUG = {
    "gemini": "google/",
    "anthropic": "anthropic/",
    "openai": "openai/",
    "xai": "x-ai/",
}


@pytest.fixture(autouse=True)
def _sin_overrides(monkeypatch):
    for tier in llm.all_tiers():
        monkeypatch.delenv(llm.ENV_PREFIX + tier.upper(), raising=False)
        monkeypatch.delenv(llm.ENV_PREFIX + tier.upper() + "_SLUG", raising=False)


def test_version_del_paquete():
    assert nickname_common.__version__ == "0.2.0"


def test_ningun_default_es_un_modelo_retirado():
    assert not _RETIRADOS & set(llm.all_models().values())


def test_ningun_slug_es_un_slug_retirado():
    slugs = {llm.router_slug(t) for t in llm.all_tiers() if llm.is_routable(t)}
    assert not _SLUGS_RETIRADOS & slugs


@pytest.mark.parametrize("tier", sorted(_REPARADOS))
def test_tiers_reparados(tier):
    modelo, slug = _REPARADOS[tier]
    assert llm.get_model(tier) == modelo
    assert llm.router_slug(tier) == slug


@pytest.mark.parametrize("tier", sorted(_NUEVOS))
def test_tiers_nuevos_de_chat(tier):
    modelo, slug, proveedor = _NUEVOS[tier]
    assert llm.get_model(tier) == modelo
    assert llm.router_slug(tier) == slug
    assert llm.provider_of(tier) == proveedor
    assert llm.is_routable(tier) is True
    assert llm.capabilities(tier) == {
        "text": True,
        "json_schema": True,
        "embeddings": False,
        "images": False,
        "thinking_budget": False,
    }


def test_gemini_embedding_no_es_de_chat_ni_enrutable():
    assert llm.get_model("gemini_embedding") == "gemini-embedding-001"
    assert llm.provider_of("gemini_embedding") == "gemini"
    assert llm.is_routable("gemini_embedding") is False
    caps = llm.capabilities("gemini_embedding")
    assert caps["embeddings"] is True
    assert caps["text"] is False
    assert caps["images"] is False
    with pytest.raises(RouterNotAvailable, match="paridad"):
        llm.router_slug("gemini_embedding")


def test_complete_legado_no_enruta_embeddings(monkeypatch):
    monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
    monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-test")
    with pytest.raises(RouterNotAvailable):
        llm.complete("gemini_embedding", [{"role": "user", "content": "x"}])


def test_imagen_sigue_siendo_el_tier_de_imagen():
    assert llm.capabilities("imagen")["images"] is True
    assert llm.is_routable("imagen") is False


def test_todo_tier_enrutable_tiene_slug_de_su_familia():
    for tier in llm.all_tiers():
        if not llm.is_routable(tier):
            continue
        assert llm.router_slug(tier).startswith(_PREFIJO_SLUG[llm.provider_of(tier)]), tier


def test_todo_tier_tiene_capacidades_y_familia():
    for tier in llm.all_tiers():
        llm.capabilities(tier)
        llm.get_model(tier)  # el default pasa su propio guard de familia


def test_provider_of_openai():
    assert llm.provider_of("gpt_quality") == "openai"
    assert llm.provider_of("gpt_fast") == "openai"


@pytest.mark.parametrize(
    "tier, mismo, cruce",
    [
        ("claude_opus", "claude-opus-9", "gpt-6-astra"),
        ("claude_haiku", "claude-haiku-9", "gemini-2.5-flash"),
        ("claude_sonnet", "claude-sonnet-9", "grok-4.7"),
        ("gpt_quality", "gpt-7", "claude-opus-5-5"),
        ("gpt_fast", "gpt-7-mini", "gemini-2.5-flash-lite"),
        ("gemini_embedding", "gemini-embedding-002", "text-embedding-3-small"),
        ("grok_fast", "grok-9", "gpt-5.4-mini"),
    ],
)
def test_guard_l014_en_tiers_nuevos_y_reparados(monkeypatch, tier, mismo, cruce):
    var = llm.ENV_PREFIX + tier.upper()
    monkeypatch.setenv(var, mismo)
    assert llm.get_model(tier) == mismo
    monkeypatch.setenv(var, cruce)
    with pytest.raises(CrossProviderOverrideError, match="familia"):
        llm.get_model(tier)


@pytest.mark.parametrize(
    "tier, mismo, cruces",
    [
        ("gemini_flash", "google/gemini-3-flash",
         ["openai/gpt-6-astra", "google/gemma-4", "gemini-3-flash"]),
        ("claude_opus", "anthropic/claude-opus-6",
         ["google/gemini-2.5-pro", "anthropic/gpt-6-astra"]),
        ("gpt_fast", "openai/gpt-6-mini",
         ["x-ai/grok-4.3", "openai/o5-mini"]),
        ("grok_quality", "x-ai/grok-5",
         ["anthropic/claude-sonnet-5.5", "x-ai/gpt-6-astra"]),
    ],
)
def test_guard_l014_tambien_en_el_override_de_slug(monkeypatch, tier, mismo, cruces):
    """NK_MODEL_<TIER>_SLUG no puede cruzar de proveedor ni de familia: el
    prefijo de OpenRouter y el nombre del modelo deben ser de la familia."""
    var = llm.ENV_PREFIX + tier.upper() + "_SLUG"
    monkeypatch.setenv(var, mismo)
    assert llm.router_slug(tier) == mismo
    for cruce in cruces:
        monkeypatch.setenv(var, cruce)
        with pytest.raises(CrossProviderOverrideError, match="familia"):
            llm.router_slug(tier)


def test_embedding_no_admite_un_modelo_de_chat_de_otra_familia(monkeypatch):
    """`text-embedding-*` es también el nombre de OpenAI: no entra en la familia."""
    monkeypatch.setenv("NK_MODEL_GEMINI_EMBEDDING", "text-embedding-004")
    with pytest.raises(CrossProviderOverrideError):
        llm.get_model("gemini_embedding")


def test_los_fallbacks_legados_no_cambian():
    """La cascada legada conserva sus defaults (vivos el 2026-10-17)."""
    assert llm.OPENAI_FALLBACK_MODEL_DEFAULT == "gpt-4o-mini"
    assert llm.ANTHROPIC_FALLBACK_MODEL_DEFAULT == "claude-haiku-4-5-20251001"
