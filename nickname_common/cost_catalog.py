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
NINGÚN precio entra aquí sin una de estas dos verificaciones para ESE modelo
exacto, con su fecha en un comentario:
  1. comparación céntimo a céntimo contra `cost_micros` de llamadas reales
     (método de `gemini-2.5-pro`, ver `.ag/api/credentials_map.md`, sección
     OpenRouter), o
  2. el `pricing` de OpenRouter, que es pass-through del precio del
     proveedor (decisión de Diego del 2026-10-18, D-F), incluidos sus
     `overrides` por tramo de contexto. OJO: leerlo del endpoint de PRIMERA
     parte (`google-ai-studio`, `anthropic`, `openai`) en
     GET /api/v1/models/<slug>/endpoints y deshacer su `discount`
     (precio / (1 - discount)). El `pricing` del listado general puede ser
     el de otro endpoint y con descuento: el 2026-10-18 daba 0,825/4,125
     USD/M para gemini-3.8-flash (vertex/us con discount 0,5) cuando Google
     cobra 1,50/7,50 (cargo upstream BYOK real).
Copiar un precio de una página pública sin esa verificación está
prohibido: produciría un "ahorro" o un "corte de presupuesto" calculado
sobre un número inventado, exactamente lo que
SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911 §17 prohíbe ("Declarar ahorro a
partir de precios teóricos sin leer el consumo real"). Un modelo ausente de
este catálogo hace que `estimate_cost_micros()` devuelva `None` — que es el
comportamiento CORRECTO (`cost_source="unavailable"`), no un bug a rellenar
con una cifra sin verificar.

Para añadir un precio nuevo: verificarlo por uno de los dos métodos de
arriba, añadir la entrada aquí CON la fecha/fuente en un comentario, y subir
`CATALOG_VERSION`.

`nickname_common.llm.complete_for` usa además este catálogo como PUERTA: con
un `BudgetGuard`, un paso directo (Anthropic, OpenAI o Gemini por su API)
cuyo modelo no esté aquí se salta en cerrado (`sin-precio`), porque esas
APIs no informan coste y el guard no podría sumar la llamada. Los respaldos
directos de Claude y GPT de la ola 1 del plano IA quedan activos con guard
desde el 2026-10-18: sus precios entraron aquí por el método 2 (ids de la
API directa, no slugs de OpenRouter).
"""

from __future__ import annotations

from dataclasses import dataclass

# Versionado explícito: cualquier LLMUsageEventV1 con cost_source="catalog"
# debería poder trazarse a la versión del catálogo que produjo esa cifra si
# el consumidor decide guardarla (este módulo no la persiste por sí mismo).
CATALOG_VERSION = "2026-10-18.1"


@dataclass(frozen=True)
class ModelPrice:
    """USD por 1.000.000 de tokens, precio de lista (pass-through OpenRouter).

    Tramo de contexto largo opcional (los `overrides` de OpenRouter con
    `min_prompt_tokens`): si la entrada alcanza `long_context_min_prompt_tokens`,
    TODA la llamada se cobra a los precios largos, como hacen los proveedores.
    """

    input_usd_per_million: float
    output_usd_per_million: float
    long_context_min_prompt_tokens: int | None = None
    long_input_usd_per_million: float | None = None
    long_output_usd_per_million: float | None = None


# Verificado 2026-08-25 contra cost_micros real de handoffs del review-fixer
# (coincidencia exacta al céntimo) — ver .ag/api/credentials_map.md, sección
# OpenRouter. Flash-Lite contrastado 2026-09-13 con tarifa oficial y cargo
# real desglosado de Google AI Studio vía OpenRouter, tier default, texto
# sin cache ni herramientas. Recibo: docs/evidence/flash-lite-cost-20260913.json.
# Flash contrastado sin generar llamadas: cargo natural de 1 petición del
# 2026-09-10 (462 tokens entrada, 47 salida, 256 microUSD reportados), tarifas
# oficiales y GET /models. Ver docs/evidence/flash-cost-20260913.json.
# No reconstruye históricos sin tokens ni convierte una estimación de catálogo
# en factura del proveedor directo. NO añadir grok-3-mini ni grok-4 sin verificar:
# mientras tanto, un evento cuyo
# proveedor no informe coste para esos modelos queda correctamente en
# cost_source="unavailable" (nunca un cero ni un precio inventado).
_PRICES: dict[str, ModelPrice] = {
    "gemini-2.5-flash": ModelPrice(
        input_usd_per_million=0.30, output_usd_per_million=2.50
    ),
    "gemini-2.5-flash-lite": ModelPrice(
        input_usd_per_million=0.10, output_usd_per_million=0.40
    ),
    "gemini-2.5-pro": ModelPrice(
        input_usd_per_million=1.25, output_usd_per_million=10.00
    ),
    # --- Método 2: `pricing` del endpoint de primera parte en OpenRouter,
    # leído el 2026-10-18 (estándar, sin flex/priority/batch ni caché). El
    # razonamiento se cobra a precio de salida (`internal_reasoning` ==
    # `completion` en los tres Gemini). D-F: Gemini 3.x del registro. Los
    # tres contrastados además por el método 1 con el smoke del 2026-10-18
    # (docs/evidence/gemini3-smoke-20261018.json: upstream_inference_cost
    # BYOK de Google AI Studio == estimación de este catálogo).
    "gemini-3.5-flash-lite": ModelPrice(
        input_usd_per_million=0.30, output_usd_per_million=2.50
    ),
    # google-ai-studio lista 0,75/3,75 con discount 0,5 (promoción de
    # OpenRouter): Google cobra 1,50/7,50, confirmado por el desglose
    # upstream_inference_prompt_cost / _completions_cost de una llamada BYOK
    # (9 tokens → 13,5 µUSD; 1 token → 7,5 µUSD).
    "gemini-3.8-flash": ModelPrice(
        input_usd_per_million=1.50, output_usd_per_million=7.50
    ),
    "gemini-3.1-pro-preview": ModelPrice(
        input_usd_per_million=2.00, output_usd_per_million=12.00,
        long_context_min_prompt_tokens=200_000,
        long_input_usd_per_million=4.00, long_output_usd_per_million=18.00,
    ),
    # Claude y GPT del registro (ids de la API directa; el slug de OpenRouter
    # es anthropic/claude-*-5.5 y openai/<id>). Mismo método y fecha.
    "claude-sonnet-5-5": ModelPrice(
        input_usd_per_million=2.00, output_usd_per_million=10.00
    ),
    "claude-opus-5-5": ModelPrice(
        input_usd_per_million=4.00, output_usd_per_million=20.00
    ),
    "claude-haiku-5-5": ModelPrice(
        input_usd_per_million=0.10, output_usd_per_million=0.50,
        long_context_min_prompt_tokens=100_000,
        long_input_usd_per_million=0.50, long_output_usd_per_million=2.50,
    ),
    "gpt-6-astra": ModelPrice(
        input_usd_per_million=10.00, output_usd_per_million=50.00,
        long_context_min_prompt_tokens=272_000,
        long_input_usd_per_million=20.00, long_output_usd_per_million=75.00,
    ),
    "gpt-5.4-mini": ModelPrice(
        input_usd_per_million=0.75, output_usd_per_million=4.50
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
    if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 0:
        raise ValueError("input_tokens/output_tokens deben ser enteros no negativos")
    in_price, out_price = price.input_usd_per_million, price.output_usd_per_million
    if (
        price.long_context_min_prompt_tokens is not None
        and input_tokens >= price.long_context_min_prompt_tokens
    ):
        in_price = price.long_input_usd_per_million
        out_price = price.long_output_usd_per_million
    usd = (input_tokens * in_price + output_tokens * out_price) / 1_000_000
    return int(round(usd * 1_000_000))
