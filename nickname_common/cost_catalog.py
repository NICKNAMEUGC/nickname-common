"""
Catálogo de coste versionado y fechado — SOLO fallback cuando el proveedor no
informa `cost_micros` en la propia respuesta (`cost_source="catalog"` en
`nickname_common.models.llm_usage.LLMUsageEventV1`).

Cuándo se usa de verdad: el leg OpenRouter (`_leg_openrouter` en `llm.py`)
pide `usage.include=True` y SÍ recibe coste real del proveedor — ese es el
camino esperado y no toca este catálogo (`cost_source="provider_response"`).
Este catálogo entra cuando la cascada cae al leg `google-direct` (API REST de
Gemini, que devuelve tokens pero NUNCA coste) — ver
SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911 §4.2, "Gemini directo puede actuar
como fallback de transporte".

Regla dura de este archivo (REGLA 0 del ecosistema — verificación agente):
NINGÚN precio entra aquí sin haber sido confirmado contra un `cost_micros`
real devuelto por un proveedor para ESE modelo exacto (mismo método que se
usó para `gemini-2.5-pro`: comparación céntimo a céntimo contra
`cost_micros` de llamadas reales — ver `.ag/api/credentials_map.md`, sección
OpenRouter). Copiar un precio de una página pública sin esa verificación
está prohibido: produciría un "ahorro" o un "corte de presupuesto" calculado
sobre un número inventado, exactamente lo que
SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911 §17 prohíbe ("Declarar ahorro a
partir de precios teóricos sin leer el consumo real"). Un modelo ausente de
este catálogo hace que `estimate_cost_micros()` devuelva `None` — que es el
comportamiento CORRECTO (`cost_source="unavailable"`), no un bug a rellenar
con una cifra sin verificar.

Para añadir un precio nuevo: verificarlo contra una respuesta real con
`usage.include=True` del leg OpenRouter para ese modelo, añadir la entrada
aquí CON la fecha/fuente de esa verificación en un comentario, y subir
`CATALOG_VERSION`.
"""

from __future__ import annotations

from dataclasses import dataclass

# Versionado explícito: cualquier LLMUsageEventV1 con cost_source="catalog"
# debería poder trazarse a la versión del catálogo que produjo esa cifra si
# el consumidor decide guardarla (este módulo no la persiste por sí mismo).
CATALOG_VERSION = "2026-09-11.1"


@dataclass(frozen=True)
class ModelPrice:
    """USD por 1.000.000 de tokens, precio de lista (pass-through OpenRouter)."""

    input_usd_per_million: float
    output_usd_per_million: float


# Verificado 2026-08-25 contra cost_micros real de handoffs del review-fixer
# (coincidencia exacta al céntimo) — ver .ag/api/credentials_map.md, sección
# OpenRouter. ÚNICO precio confirmado en el ecosistema a fecha de este
# módulo. NO añadir gemini-2.5-flash, gemini-2.5-flash-lite, grok-3-mini ni
# grok-4 aquí hasta verificarlos igual: mientras tanto, un evento cuyo
# proveedor no informe coste para esos modelos queda correctamente en
# cost_source="unavailable" (nunca un cero ni un precio inventado).
_PRICES: dict[str, ModelPrice] = {
    "gemini-2.5-pro": ModelPrice(
        input_usd_per_million=1.25, output_usd_per_million=10.00
    ),
}


def known_models() -> tuple[str, ...]:
    """Modelos con precio verificado en este catálogo."""
    return tuple(sorted(_PRICES))


def estimate_cost_micros(
    model: str,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
) -> int | None:
    """Estima el coste en micro-USD (1 USD = 1_000_000 micros) desde el
    catálogo verificado.

    Devuelve `None` — nunca 0 — cuando falta el precio del modelo o falta
    cualquiera de los dos recuentos de tokens. `None` es la señal correcta
    de "no se puede calcular", no un error a silenciar.
    """
    price = _PRICES.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("input_tokens/output_tokens no pueden ser negativos")
    usd = (
        input_tokens * price.input_usd_per_million
        + output_tokens * price.output_usd_per_million
    ) / 1_000_000
    return int(round(usd * 1_000_000))
