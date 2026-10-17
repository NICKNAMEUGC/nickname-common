"""Tests del catálogo de coste versionado (WI-1).

Regla que estos tests protegen: NINGÚN modelo sin precio verificado devuelve
una cifra — devuelve None. Un test que "arreglara" esto poniendo un precio
inventado para que pase estaría violando la razón de ser del propio módulo.
"""

from nickname_common import cost_catalog


def test_known_model_computes_expected_micros():
    # gemini-2.5-pro: 1.25 USD/M in + 10.00 USD/M out (verificado, ver
    # docstring del módulo). 1000 tokens in + 1000 tokens out:
    #   1000/1e6 * 1.25 + 1000/1e6 * 10.00 = 0.00125 + 0.01 = 0.01125 USD
    #   = 11250 micro-USD
    got = cost_catalog.estimate_cost_micros(
        "gemini-2.5-pro", input_tokens=1000, output_tokens=1000
    )
    assert got == 11250


def test_zero_tokens_is_zero_not_none():
    got = cost_catalog.estimate_cost_micros(
        "gemini-2.5-pro", input_tokens=0, output_tokens=0
    )
    assert got == 0


def test_unknown_model_returns_none():
    assert (
        cost_catalog.estimate_cost_micros(
            "unverified-model", input_tokens=100, output_tokens=50
        )
        is None
    ), "Un modelo no verificado conserva coste desconocido"


def test_missing_input_tokens_returns_none():
    assert (
        cost_catalog.estimate_cost_micros(
            "gemini-2.5-pro", input_tokens=None, output_tokens=50
        )
        is None
    )


def test_missing_output_tokens_returns_none():
    assert (
        cost_catalog.estimate_cost_micros(
            "gemini-2.5-pro", input_tokens=50, output_tokens=None
        )
        is None
    )


def test_negative_tokens_raise():
    import pytest

    with pytest.raises(ValueError):
        cost_catalog.estimate_cost_micros(
            "gemini-2.5-pro", input_tokens=-1, output_tokens=10
        )


def test_known_models_lists_only_verified_entries():
    assert cost_catalog.known_models() == (
        "claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5",
        "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
        "gemini-3.1-pro-preview", "gemini-3.5-flash-lite", "gemini-3.8-flash",
        "gpt-5.4-mini", "gpt-6-astra",
    )


def test_gemini_3_precios_de_openrouter_20261018():
    """D-F: precios del campo `pricing` de OpenRouter (pass-through), 2026-10-18."""
    assert cost_catalog.estimate_cost_micros(
        "gemini-3.5-flash-lite", input_tokens=1_000_000, output_tokens=0) == 300_000
    assert cost_catalog.estimate_cost_micros(
        "gemini-3.8-flash", input_tokens=0, output_tokens=1_000_000) == 7_500_000
    # Cargo upstream BYOK real del 2026-10-18: 9 tokens de entrada + 1 de salida = 21 µUSD.
    assert cost_catalog.estimate_cost_micros(
        "gemini-3.8-flash", input_tokens=9, output_tokens=1) == 21
    assert cost_catalog.estimate_cost_micros(
        "gemini-3.1-pro-preview", input_tokens=1000, output_tokens=1000) == 14_000


def test_tramo_de_contexto_largo_cobra_toda_la_llamada_al_precio_largo():
    # gemini-3.1-pro-preview: override de OpenRouter min_prompt_tokens=200000 → 4/18 USD/M.
    assert cost_catalog.estimate_cost_micros(
        "gemini-3.1-pro-preview", input_tokens=199_999, output_tokens=0) == 399_998
    assert cost_catalog.estimate_cost_micros(
        "gemini-3.1-pro-preview", input_tokens=200_000, output_tokens=1000) == 818_000
    assert cost_catalog.estimate_cost_micros(
        "gpt-6-astra", input_tokens=272_000, output_tokens=0) == 5_440_000
    assert cost_catalog.estimate_cost_micros(
        "claude-haiku-5-5", input_tokens=100_000, output_tokens=0) == 50_000


def test_flash_catalog_matches_natural_single_request_charge():
    import json
    from decimal import Decimal
    from pathlib import Path
    receipt = json.loads((Path(__file__).parents[1] / "docs/evidence/flash-cost-20260913.json").read_text())
    row = receipt["natural_usage"]
    assert row["requests"] == 1 and receipt["synthetic_calls"] == 0
    assert row["model"] == "google/gemini-2.5-flash"
    actual_micros = Decimal(str(row["usage"])) * 1_000_000
    assert cost_catalog.estimate_cost_micros("gemini-2.5-flash",
        input_tokens=row["prompt_tokens"], output_tokens=row["completion_tokens"]) == actual_micros
    assert cost_catalog.estimate_cost_micros("gemini-2.5-flash",
        input_tokens=None, output_tokens=1) is None


def test_catalog_version_is_a_dated_string():
    assert cost_catalog.CATALOG_VERSION[:10].count("-") == 2  # YYYY-MM-DD...


def test_flash_lite_matches_actual_provider_charge_and_individual_rates():
    import json
    from decimal import Decimal
    from pathlib import Path

    record = json.loads((Path(__file__).parents[1] / "docs/evidence/flash-lite-cost-20260913.json").read_text())
    usage, costs = record["usage"], record["cost_details"]
    assert record["requested_model"] == record["returned_model"] == "google/gemini-2.5-flash-lite"
    assert record["provider"] == "Google AI Studio" and record["service_tier"] == "default"
    assert Decimal(str(costs["upstream_inference_prompt_cost"])) / usage["prompt_tokens"] == Decimal("0.0000001")
    assert Decimal(str(costs["upstream_inference_completions_cost"])) / usage["completion_tokens"] == Decimal("0.0000004")
    actual = Decimal(str(costs["upstream_inference_cost"])) * 1_000_000
    assert cost_catalog.estimate_cost_micros("gemini-2.5-flash-lite",
        input_tokens=usage["prompt_tokens"], output_tokens=usage["completion_tokens"]) == round(actual)
    assert record["readback"]["total_cost"] == usage["cost"]
    assert cost_catalog.estimate_cost_micros("gemini-2.5-flash-lite",
        input_tokens=1000, output_tokens=1000) == 500


def test_invalid_counters_cannot_become_a_cost():
    import pytest
    for invalid in (True, 1.5, "12", float("nan")):
        with pytest.raises(ValueError):
            cost_catalog.estimate_cost_micros("gemini-2.5-flash-lite", input_tokens=invalid, output_tokens=1)
