"""Política del plano IA por clase (SPEC-AI-PLANE-20261017 §4, ola 1).

`nickname_common.ai_policy` es la fuente canónica. Estos tests fijan las
cadenas que Diego decidió el 2026-10-17 (D2, D3, D-A) y que la validación
rechaza cualquier política incoherente con el registro de modelos.
"""

import json

import pytest

from nickname_common import ai_policy, llm
from nickname_common.ai_policy import (
    ApiStep,
    ClassPolicy,
    PolicyError,
    SubscriptionStep,
)

_CLASES = (
    "clasificacion_masiva",
    "cliente_dinero_tiempo_real",
    "cliente_dinero_asincrono",
    "codigo",
    "revision",
    "contexto_largo",
    "coordinacion",
    "embeddings",
    "imagen",
)

_CLIENTE_DINERO = [
    ("claude_sonnet", "openrouter"),
    ("claude_sonnet", "anthropic_direct"),
    ("gpt_quality", "openrouter"),
    ("gpt_quality", "openai_direct"),
]

_CADENAS_API = {
    "clasificacion_masiva": [
        ("gemini_flash_lite", "openrouter"),
        ("gemini_flash_lite", "gemini_direct"),
        ("gemini_flash", "openrouter"),
        ("gemini_flash", "gemini_direct"),
    ],
    "cliente_dinero_tiempo_real": _CLIENTE_DINERO,
    "cliente_dinero_asincrono": _CLIENTE_DINERO,
    "codigo": [],
    "revision": [],
    "contexto_largo": [
        ("gemini_pro", "openrouter"),
        ("gemini_pro", "gemini_direct"),
        ("claude_sonnet", "openrouter"),
    ],
    "coordinacion": [],
    "embeddings": [("gemini_embedding", "gemini_direct")],
    "imagen": [("imagen", "gemini_direct")],
}

_CADENAS_SUSCRIPCION = {
    "clasificacion_masiva": [],
    "cliente_dinero_tiempo_real": [],
    "cliente_dinero_asincrono": ["claude", "codex"],
    "codigo": ["claude", "codex", "grok"],
    "revision": ["codex", "claude", "grok"],
    "contexto_largo": ["gemini", "claude"],
    "coordinacion": ["grok"],
    "embeddings": [],
    "imagen": [],
}

_CARRILES = {
    "clasificacion_masiva": "api",
    "cliente_dinero_tiempo_real": "api",
    "cliente_dinero_asincrono": "subscription",
    "codigo": "subscription",
    "revision": "subscription",
    "contexto_largo": "subscription",
    "coordinacion": "subscription",
    "embeddings": "api",
    "imagen": "api",
}


def test_clases_declaradas_en_orden():
    assert ai_policy.all_classes() == _CLASES


@pytest.mark.parametrize("clase", _CLASES)
def test_cadena_api_de_cada_clase(clase):
    assert [(s.tier, s.via) for s in ai_policy.api_chain(clase)] == _CADENAS_API[clase]


@pytest.mark.parametrize("clase", _CLASES)
def test_cadena_de_suscripcion_de_cada_clase(clase):
    assert [s.cli for s in ai_policy.subscription_chain(clase)] == _CADENAS_SUSCRIPCION[clase]


@pytest.mark.parametrize("clase", _CLASES)
def test_carril_de_cada_clase(clase):
    assert ai_policy.policy_for(clase).lane == _CARRILES[clase]


def test_cliente_dinero_empieza_por_claude_byok_y_tiene_respaldo_directo():
    """D-A: Claude por OpenRouter (BYOK → crédito Team) y, solo donde haya
    ANTHROPIC_API_KEY, Claude directo como respaldo antes de GPT."""
    pasos = ai_policy.api_chain("cliente_dinero_tiempo_real")
    assert pasos[0] == ApiStep("claude_sonnet", "openrouter")
    assert pasos[1] == ApiStep("claude_sonnet", "anthropic_direct")
    assert all(llm.provider_of(p.tier) in {"anthropic", "openai"} for p in pasos), (
        "D2: nada que toque clientes o dinero cae a Gemini"
    )


def test_gemini_solo_es_primario_en_clasificacion_masiva():
    for clase in ai_policy.all_classes():
        pasos = ai_policy.api_chain(clase)
        if not pasos or clase in {"clasificacion_masiva", "contexto_largo", "embeddings", "imagen"}:
            continue
        assert llm.provider_of(pasos[0].tier) != "gemini", clase


def test_el_asincrono_respalda_con_la_misma_cadena_que_el_tiempo_real():
    assert ai_policy.api_chain("cliente_dinero_asincrono") == ai_policy.api_chain(
        "cliente_dinero_tiempo_real"
    )


def test_revision_declara_la_regla_de_familia_distinta_y_los_roles():
    pol = ai_policy.policy_for("revision")
    assert ai_policy.RULE_DIFFERENT_FAMILY_FROM_AUTHOR in pol.broker_rules
    assert ai_policy.RULE_DIFFERENT_FAMILY_FROM_AUTHOR in ai_policy.BROKER_RULES
    assert [s.role for s in pol.subscription_chain] == ["revisor", "revisor", "tercera_opinion"]


def test_todo_tier_referenciado_existe_en_el_registro():
    tiers = set(llm.all_tiers())
    for clase in ai_policy.all_classes():
        for paso in ai_policy.api_chain(clase):
            assert paso.tier in tiers, (clase, paso)


def test_embeddings_e_imagen_nunca_van_por_openrouter():
    for clase in ai_policy.all_classes():
        for paso in ai_policy.api_chain(clase):
            if paso.tier in {"gemini_embedding", "imagen"}:
                assert paso.via != "openrouter", (clase, paso)


def test_la_politica_habla_de_tiers_no_de_modelos():
    """Ningún nombre de modelo del registro aparece en la política exportada."""
    volcado = json.dumps(ai_policy.as_dict())
    for modelo in llm.all_models().values():
        assert f'"{modelo}"' not in volcado, modelo


def test_as_dict_es_serializable_y_versionado():
    datos = ai_policy.as_dict()
    assert datos["policy_version"] == ai_policy.POLICY_VERSION
    assert set(datos["classes"]) == set(_CLASES)
    json.dumps(datos)  # no lanza


def test_clase_desconocida_falla_con_la_lista_valida():
    with pytest.raises(KeyError, match="clasificacion_masiva"):
        ai_policy.policy_for("nope")
    with pytest.raises(KeyError):
        ai_policy.api_chain("nope")
    with pytest.raises(KeyError):
        ai_policy.subscription_chain("nope")


def test_la_politica_canonica_valida():
    ai_policy.validate()


def _pol(**kw):
    base = dict(clase="x", lane="api", description="d",
                api_chain=(ApiStep("gemini_flash", "openrouter"),))
    base.update(kw)
    return {base["clase"]: ClassPolicy(**base)}


@pytest.mark.parametrize(
    "politica, motivo",
    [
        (_pol(api_chain=(ApiStep("gpt5", "openrouter"),)), "no existe"),
        (_pol(api_chain=(ApiStep("gemini_embedding", "openrouter"),)), "no se enruta"),
        (_pol(api_chain=(ApiStep("imagen", "openrouter"),)), "no se enruta"),
        (_pol(api_chain=(ApiStep("gemini_flash", "anthropic_direct"),)), "no sirve"),
        (_pol(api_chain=(ApiStep("claude_sonnet", "openai_direct"),)), "no sirve"),
        (_pol(api_chain=(ApiStep("gpt_quality", "gemini_direct"),)), "no sirve"),
        (_pol(api_chain=(ApiStep("grok_fast", "anthropic_direct"),)), "no sirve"),
        (_pol(api_chain=(ApiStep("gemini_flash", "telepatia"),)), "vía"),
        (_pol(api_chain=(ApiStep("gemini_flash", "openrouter"),
                         ApiStep("gemini_flash", "openrouter"))), "repetido"),
        (_pol(api_chain=()), "sin cadena API"),
        (_pol(lane="subscription", subscription_chain=()), "sin cadena de suscripción"),
        (_pol(lane="batch"), "lane"),
        (_pol(lane="subscription", subscription_chain=(SubscriptionStep("copilot"),)), "CLI"),
        (_pol(broker_rules=("inventada",)), "regla"),
        ({"y": ClassPolicy(clase="x", lane="api", description="d",
                           api_chain=(ApiStep("gemini_flash", "openrouter"),))}, "distinta"),
    ],
)
def test_validate_rechaza_politicas_incoherentes(politica, motivo):
    with pytest.raises(PolicyError, match=motivo):
        ai_policy.validate(politica)
