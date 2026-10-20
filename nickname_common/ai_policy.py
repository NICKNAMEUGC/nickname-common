"""Política del plano IA por clase de trabajo — FUENTE CANÓNICA.

SPEC-AI-PLANE-20261017 §4 (ola 1), decisiones de Diego del 2026-10-17:

- D1: plano IA declarativo, `nickname_common.llm` como único cliente API y un
  broker único de suscripciones en el Mac Mini (ola 4). Sin gateway nuevo.
- D2: coste marginal cero primero, PERO con el modelo adecuado a cada caso:
  Claude/GPT de gama alta para todo lo que toque clientes o dinero; Gemini
  solo para clasificación masiva; OpenRouter cuando todo lo demás falla.
- D3: los agentes de negocio delegan en suscripciones solo trabajo
  asíncrono; el tiempo real sigue por API.
- D4: pool rotativo de cuentas de suscripción (lo aplica el broker).
- D-A (híbrido): el crédito de API del plan Team de Anthropic se consume por
  BYOK en OpenRouter como vía normal (cada agente conserva su clave de
  OpenRouter con tope diario y el gasto Claude se factura a la organización
  de Anthropic, que lo absorbe con el crédito Team); ADEMÁS, clave Anthropic
  directa como respaldo solo en los agentes críticos de dinero
  (nickname-recobro-agent, nickname-facturas-agent y los jobs CFO de
  nickname-odoo-agent). En código eso es el paso
  `claude_sonnet@anthropic_direct` más la lista `ANTHROPIC_DIRECT_AUTHORIZED`:
  el paso solo se ejecuta si existe `ANTHROPIC_API_KEY` Y el servicio
  (`NK_AGENT_NAME`, y en odoo-agent también el `job_id`) está autorizado. La
  clave sola no basta: la misma clave compartida existe en otros servicios
  (ver `.ag/api/credentials_map.md`), y en ellos el paso se salta.

Los dos papeles de OpenRouter en las cadenas (D2 + D-A):

- `claude_*@openrouter` es la vía NORMAL de Claude: el BYOK la factura a la
  organización de Anthropic y la absorbe el crédito Team (coste marginal
  cero). Por eso va primero en las clases de cliente/dinero.
- `gemini_*@openrouter` y `gpt_*@openrouter` son el ÚLTIMO RECURSO medido de
  D2 («OpenRouter cuando todo lo demás falla»): Gemini y GPT no tienen
  crédito, así que la vía directa del mismo tier va antes y OpenRouter
  queda detrás de todo lo demás (SPEC §4, columna «Último recurso»). La
  vía directa no queda sin control de gasto: `complete_for` exige
  `BudgetGuard` y salta en cerrado cualquier paso directo sin precio
  verificado en `nickname_common.cost_catalog`.

Por qué vive aquí y no en un YAML de `nickname-apps-config`: los servicios de
Railway no tienen ese repo, y este paquete sí viaja con cada agente. Solo
stdlib, como el resto del paquete. Si en el futuro se exporta a YAML para el
broker o el Command Center, el YAML se GENERA desde aquí (`as_dict()`), no al
revés.

Reglas de forma (se validan al importar el módulo, y `nickname_common.llm`
lo importa al cargarse: un error aquí rompe el arranque del servicio, no la
primera llamada a `complete_for`):

- Los nombres de modelo NUNCA aparecen aquí: la política habla de tiers del
  registro (`nickname_common.llm`).
- Cada paso API es `{tier, via}` con `via` en VIAS. La vía debe ser coherente
  con el tier: `openrouter` solo para tiers enrutables (embeddings e imagen
  nunca), y cada vía directa solo para tiers de su familia.
- Cada paso de suscripción es `{cli, role?}` con `cli` en CLIS. El broker de
  la ola 4 elige la cuenta del pool dentro de cada CLI.
- `lane="api"` exige cadena API; `lane="subscription"` exige cadena de
  suscripción (la cadena API, si existe, es el respaldo cuando el Mini no
  está o no hay cuota).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from nickname_common import llm

POLICY_VERSION = "2026-10-20.1"

LANES = ("api", "subscription")
VIAS = ("openrouter", "anthropic_direct", "openai_direct", "gemini_direct")
# `agy` es Antigravity CLI: Google cerró Gemini CLI a las cuentas particulares
# (AI Pro y Ultra incluidas) el 18/06/2026 y su sustituto oficial es Antigravity.
CLIS = ("claude", "codex", "agy", "grok")

# Familia de proveedor que admite cada vía directa (llm.provider_of).
_DIRECT_VIA_PROVIDER = {
    "anthropic_direct": "anthropic",
    "openai_direct": "openai",
    "gemini_direct": "gemini",
}

# Reglas que aplica el broker de suscripciones (ola 4). Aquí solo se declaran.
RULE_DIFFERENT_FAMILY_FROM_AUTHOR = "familia_distinta_al_autor"
RULE_OPENROUTER_LAST_RESORT_QUOTA = "openrouter_ultimo_recurso_cuota_agotada"
BROKER_RULES = {
    RULE_DIFFERENT_FAMILY_FROM_AUTHOR: (
        "El revisor debe ser de una familia distinta a la del autor del cambio: "
        "el broker descarta de la cadena el CLI de la misma familia que el autor "
        "y usa el siguiente. Grok queda como tercera opinión."
    ),
    RULE_OPENROUTER_LAST_RESORT_QUOTA: (
        "OpenRouter es el último recurso de la clase, fuera de su cadena API: "
        "solo cuando todas las cuentas candidatas del pool estén enfriadas con "
        "evidencia explícita de cuota agotada y el flag nominal "
        "ENGINEERING_OPENROUTER_FALLBACK_ENABLED esté activo. Falta de login, "
        "host caído, timeout o error de red no acreditan cuota agotada. Regla "
        "vigente de docs/SUBSCRIPTION-EXECUTORS.md (nickname-apps-config) "
        "llevada al pool (SPEC-AI-PLANE-20261017 §4 y §6)."
    ),
}

# D-A: servicios autorizados a usar la vía `anthropic_direct` (respaldo con la
# clave Anthropic directa cuando OpenRouter cae). Clave: `NK_AGENT_NAME` del
# despliegue. Valor: None = todo el servicio; tupla = solo los `job_id` que
# empiecen por alguno de esos prefijos (en odoo-agent, únicamente los jobs
# CFO, que se identifican con un job_id `cfo...`). Sin entrada aquí, el paso
# se salta aunque el servicio tenga ANTHROPIC_API_KEY.
ANTHROPIC_DIRECT_AUTHORIZED: Mapping[str, tuple[str, ...] | None] = MappingProxyType({
    "nickname-recobro-agent": None,
    "nickname-facturas-agent": None,
    "nickname-odoo-agent": ("cfo",),
})


class PolicyError(ValueError):
    """La política declarada es incoherente (tier, vía, carril o CLI)."""


@dataclass(frozen=True)
class ApiStep:
    """Un paso de la cadena API: qué tier y por qué puerta."""

    tier: str
    via: str

    def as_dict(self) -> dict:
        return {"tier": self.tier, "via": self.via}


@dataclass(frozen=True)
class SubscriptionStep:
    """Un paso de la cadena de suscripción: qué CLI y, si hace falta, su rol."""

    cli: str
    role: str | None = None

    def as_dict(self) -> dict:
        data = {"cli": self.cli}
        if self.role is not None:
            data["role"] = self.role
        return data


@dataclass(frozen=True)
class ClassPolicy:
    """Política completa de una clase de trabajo."""

    clase: str
    lane: str
    description: str
    api_chain: tuple[ApiStep, ...] = ()
    subscription_chain: tuple[SubscriptionStep, ...] = ()
    broker_rules: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "clase": self.clase,
            "lane": self.lane,
            "description": self.description,
            "api_chain": [s.as_dict() for s in self.api_chain],
            "subscription_chain": [s.as_dict() for s in self.subscription_chain],
            "broker_rules": list(self.broker_rules),
        }


def _api(*pairs: str) -> tuple[ApiStep, ...]:
    """`_api("tier@via", ...)` → tupla de ApiStep (legible en la tabla)."""
    steps = []
    for pair in pairs:
        tier, _, via = pair.partition("@")
        steps.append(ApiStep(tier=tier, via=via))
    return tuple(steps)


def _subs(*clis: str | tuple[str, str]) -> tuple[SubscriptionStep, ...]:
    return tuple(
        SubscriptionStep(cli=c) if isinstance(c, str) else SubscriptionStep(cli=c[0], role=c[1])
        for c in clis
    )


# Cadena de cliente/dinero (D2 + D-A, SPEC §4): Claude Sonnet por OpenRouter
# con BYOK (lo absorbe el crédito Team) → Claude Sonnet con clave directa
# (solo en los agentes críticos autorizados por D-A) → GPT de gama alta
# directo → GPT de gama alta por OpenRouter, último recurso medido (D2).
_CLIENTE_DINERO_API = _api(
    "claude_sonnet@openrouter",
    "claude_sonnet@anthropic_direct",
    "gpt_quality@openai_direct",
    "gpt_quality@openrouter",
)

_POLICY: dict[str, ClassPolicy] = {
    p.clase: p
    for p in (
        ClassPolicy(
            clase="clasificacion_masiva",
            lane="api",
            description=(
                "Triaje de alto volumen sin redacción de cara al cliente "
                "(management-gmail, EmilIA, tickets, feedback). Única clase de "
                "chat por API con Gemini como primario (D2); Gemini también es "
                "primario en contexto_largo, embeddings e imagen. Flash-Lite, "
                "después Flash y OpenRouter como último recurso (SPEC §4)."
            ),
            api_chain=_api(
                "gemini_flash_lite@gemini_direct",
                "gemini_flash@gemini_direct",
                "gemini_flash_lite@openrouter",
                "gemini_flash@openrouter",
            ),
        ),
        ClassPolicy(
            clase="cliente_dinero_tiempo_real",
            lane="api",
            description=(
                "Respuestas a clientes, recobro, facturas, Odoo y propuestas en "
                "tiempo real. Claude/GPT de gama alta (D2); tiempo real siempre "
                "por API (D3)."
            ),
            api_chain=_CLIENTE_DINERO_API,
        ),
        ClassPolicy(
            clase="cliente_dinero_asincrono",
            lane="subscription",
            description=(
                "Borradores con aprobación, informes CFO y análisis. Primario por "
                "suscripción en el Mini (D3, D4); la cadena API es el respaldo "
                "cuando el Mini no está o no hay cuota."
            ),
            api_chain=_CLIENTE_DINERO_API,
            subscription_chain=_subs("claude", "codex"),
        ),
        ClassPolicy(
            clase="codigo",
            lane="subscription",
            description=(
                "Handoffs, Review Fixer como autor y Symphony. Sin cadena API: "
                "OpenRouter solo con cuota agotada (regla del broker)."
            ),
            subscription_chain=_subs("claude", "codex", "grok"),
            broker_rules=(RULE_OPENROUTER_LAST_RESORT_QUOTA,),
        ),
        ClassPolicy(
            clase="revision",
            lane="subscription",
            description=(
                "Revisión de PR y auditorías. El revisor es de familia distinta "
                "a la del autor; Grok como tercera opinión."
            ),
            subscription_chain=_subs(
                ("codex", "revisor"),
                ("claude", "revisor"),
                ("grok", "tercera_opinion"),
            ),
            broker_rules=(RULE_DIFFERENT_FAMILY_FROM_AUTHOR,),
        ),
        ClassPolicy(
            clase="contexto_largo",
            lane="subscription",
            description=(
                "Auditorías documentales, destilado de Obsidian y Drive. "
                "Antigravity CLI (Gemini Ultra) primario; por API, Gemini Pro directo, después "
                "Claude (BYOK, crédito Team) y OpenRouter como último recurso "
                "(SPEC §4)."
            ),
            api_chain=_api(
                "gemini_pro@gemini_direct",
                "claude_sonnet@openrouter",
                "gemini_pro@openrouter",
            ),
            subscription_chain=_subs("agy", "claude"),
        ),
        ClassPolicy(
            clase="coordinacion",
            lane="subscription",
            description="Asientos de Grok Bot (plano de coordinación executor:grok).",
            subscription_chain=_subs("grok"),
        ),
        ClassPolicy(
            clase="embeddings",
            lane="api",
            description=(
                "RAG de Nicky e ig. No es chat: se llama por :embedContent con "
                "get_model('gemini_embedding'); complete_for la rechaza."
            ),
            api_chain=_api("gemini_embedding@gemini_direct"),
        ),
        ClassPolicy(
            clase="imagen",
            lane="api",
            description=(
                "Carruseles y n8n con el modelo de imagen Gemini vigente. No es "
                "chat: complete_for la rechaza."
            ),
            api_chain=_api("imagen@gemini_direct"),
        ),
    )
}


def all_classes() -> tuple[str, ...]:
    """Clases de trabajo declaradas, en el orden de la política."""
    return tuple(_POLICY)


def policy_for(clase: str) -> ClassPolicy:
    """Política completa de la clase. KeyError si no existe."""
    try:
        return _POLICY[clase]
    except KeyError:
        raise KeyError(
            f"Clase de trabajo desconocida: {clase!r}. Válidas: {list(_POLICY)}"
        ) from None


def api_chain(clase: str) -> tuple[ApiStep, ...]:
    """Cadena API ordenada de la clase (vacía si es solo de suscripción)."""
    return policy_for(clase).api_chain


def via_serves_tier(via: str, tier: str) -> bool:
    """¿Puede la vía servir el tier? (openrouter: enrutable; directa: su familia)."""
    if via == "openrouter":
        return llm.is_routable(tier)
    return _DIRECT_VIA_PROVIDER.get(via) == llm.provider_of(tier)


def api_chain_for_tier(clase: str, tier: str) -> tuple[ApiStep, ...]:
    """Cadena API de la clase con el tier FIJADO (fase «solo transporte», D-E).

    Mismas vías que la clase y en su orden, cada una una sola vez y solo si
    sirve al tier. Si la clase tiene pasos de la MISMA familia que el tier,
    se toma el orden de vías de esos pasos (D2: Claude por OpenRouter
    primero; Gemini y GPT, directo primero); si no, el de todos los pasos.
    Si ninguna vía de la clase le sirve (o la clase no tiene cadena API), la
    cadena legado del tier: `openrouter` (si es enrutable) → vía directa de
    su familia (si existe; xAI no tiene). D-E: cambiar de FAMILIA exige eval;
    esto solo fija el tier que el consumidor ya usa, sin elegir otro.
    """
    if tier not in llm.all_tiers():
        raise KeyError(f"Tier de modelo desconocido: {tier!r}")
    chain = api_chain(clase)
    family = llm.provider_of(tier)
    same_family = [s for s in chain if llm.provider_of(s.tier) == family]
    steps: list[ApiStep] = []
    seen: set[str] = set()
    for step in (same_family or chain):
        if step.via in seen or not via_serves_tier(step.via, tier):
            continue
        seen.add(step.via)
        steps.append(ApiStep(tier=tier, via=step.via))
    if steps:
        return tuple(steps)
    legacy = [via for via in ("openrouter", *_DIRECT_VIA_PROVIDER) if via_serves_tier(via, tier)]
    return tuple(ApiStep(tier=tier, via=via) for via in legacy)


def subscription_chain(clase: str) -> tuple[SubscriptionStep, ...]:
    """Cadena de CLIs de suscripción de la clase, para el broker (ola 4)."""
    return policy_for(clase).subscription_chain


def anthropic_direct_allowed(service: str | None, job_id: str | None) -> bool:
    """¿Puede este servicio (y este job) usar la vía `anthropic_direct`? (D-A)

    `service` es la identidad del despliegue (`NK_AGENT_NAME`). Un servicio
    fuera de `ANTHROPIC_DIRECT_AUTHORIZED` nunca la usa, tenga o no la clave.
    """
    if not service or service not in ANTHROPIC_DIRECT_AUTHORIZED:
        return False
    prefixes = ANTHROPIC_DIRECT_AUTHORIZED[service]
    if prefixes is None:
        return True
    return bool(job_id) and job_id.startswith(prefixes)


def as_dict() -> dict:
    """La política entera serializable (para exportarla, nunca para editarla)."""
    return {
        "policy_version": POLICY_VERSION,
        "classes": {name: p.as_dict() for name, p in _POLICY.items()},
        "broker_rules": dict(BROKER_RULES),
        "anthropic_direct_authorized": {
            svc: (list(scope) if scope is not None else None)
            for svc, scope in ANTHROPIC_DIRECT_AUTHORIZED.items()
        },
    }


def validate_anthropic_direct(authorized: Mapping[str, tuple[str, ...] | None]) -> None:
    """Comprueba la forma de la lista D-A (servicio → None o prefijos de job)."""
    for service, scope in authorized.items():
        if not isinstance(service, str) or not service.strip():
            raise PolicyError(f"D-A: servicio inválido {service!r}")
        if scope is None:
            continue
        if not isinstance(scope, tuple) or not scope or not all(
            isinstance(p, str) and p.strip() for p in scope
        ):
            raise PolicyError(
                f"D-A: {service}: el alcance debe ser None o una tupla de prefijos "
                f"de job_id no vacíos, no {scope!r}"
            )


def validate(policy: dict[str, ClassPolicy] | None = None) -> None:
    """Comprueba la coherencia de la política contra el registro de modelos.

    Se llama al importar el módulo con la política canónica; los tests la
    llaman con políticas inválidas para comprobar que se rechazan.
    """
    policy = _POLICY if policy is None else policy
    tiers = set(llm.all_tiers())
    for name, p in policy.items():
        if name != p.clase:
            raise PolicyError(f"Clave {name!r} distinta de clase={p.clase!r}")
        if p.lane not in LANES:
            raise PolicyError(f"{name}: lane {p.lane!r} no está en {LANES}")
        if p.lane == "api" and not p.api_chain:
            raise PolicyError(f"{name}: lane=api sin cadena API")
        if p.lane == "subscription" and not p.subscription_chain:
            raise PolicyError(f"{name}: lane=subscription sin cadena de suscripción")
        seen = set()
        for step in p.api_chain:
            if step.tier not in tiers:
                raise PolicyError(f"{name}: tier {step.tier!r} no existe en el registro")
            if step.via not in VIAS:
                raise PolicyError(f"{name}: vía {step.via!r} no está en {VIAS}")
            if (step.tier, step.via) in seen:
                raise PolicyError(f"{name}: paso repetido {step.tier}@{step.via}")
            seen.add((step.tier, step.via))
            if step.via == "openrouter":
                if not llm.is_routable(step.tier):
                    raise PolicyError(
                        f"{name}: el tier {step.tier!r} no se enruta por OpenRouter "
                        "(embeddings/imagen van directos)"
                    )
            elif llm.provider_of(step.tier) != _DIRECT_VIA_PROVIDER[step.via]:
                raise PolicyError(
                    f"{name}: la vía {step.via!r} no sirve el tier {step.tier!r} "
                    f"(familia {llm.provider_of(step.tier)!r})"
                )
        for step in p.subscription_chain:
            if step.cli not in CLIS:
                raise PolicyError(f"{name}: CLI {step.cli!r} no está en {CLIS}")
        for rule in p.broker_rules:
            if rule not in BROKER_RULES:
                raise PolicyError(f"{name}: regla de broker desconocida {rule!r}")


validate()
validate_anthropic_direct(ANTHROPIC_DIRECT_AUTHORIZED)
