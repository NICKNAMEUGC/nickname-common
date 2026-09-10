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
            "gemini-2.5-flash-lite", input_tokens=100, output_tokens=50
        )
        is None
    ), "flash-lite no tiene precio verificado todavía — None es correcto, no un bug"


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
    assert cost_catalog.known_models() == ("gemini-2.5-pro",)


def test_catalog_version_is_a_dated_string():
    assert cost_catalog.CATALOG_VERSION[:10].count("-") == 2  # YYYY-MM-DD...
