"""Tests de fingerprint() e idempotency_key() (WI-1).

La huella es la pieza que permite "cero LLM cuando no hay novedades" (SPEC
§7.2): si esto falla en silencio (huella inestable con el mismo contenido),
un checker dispara análisis LLM en cada tick aunque nada haya cambiado.
"""

import pytest

from nickname_common.fingerprint import fingerprint, idempotency_key


def test_same_content_same_fingerprint_regardless_of_key_order():
    a = {"sha": "abc123", "ci": "green", "pr": 42}
    b = {"pr": 42, "sha": "abc123", "ci": "green"}
    assert fingerprint(a) == fingerprint(b)


def test_different_content_different_fingerprint():
    a = {"sha": "abc123", "ci": "green"}
    b = {"sha": "abc124", "ci": "green"}
    assert fingerprint(a) != fingerprint(b)


def test_fingerprint_is_deterministic_across_calls():
    obj = {"a": [1, 2, {"b": None, "c": True}], "d": "texto"}
    assert fingerprint(obj) == fingerprint(obj)


def test_fingerprint_nested_list_order_matters():
    # A diferencia de las claves de dict, el orden de una LISTA sí es
    # contenido real (p.ej. una secuencia de eventos) — no se normaliza.
    assert fingerprint([1, 2, 3]) != fingerprint([3, 2, 1])


def test_fingerprint_returns_hex_sha256():
    h = fingerprint({"x": 1})
    assert len(h) == 64
    int(h, 16)  # no lanza si es hex válido


class TestIdempotencyKey:
    def test_joins_parts_with_colon(self):
        assert idempotency_key("feedback", "2026-09-10") == "feedback:2026-09-10"

    def test_single_part(self):
        assert idempotency_key("solo") == "solo"

    def test_requires_at_least_one_part(self):
        with pytest.raises(ValueError):
            idempotency_key()

    def test_rejects_empty_part(self):
        with pytest.raises(ValueError):
            idempotency_key("feedback", "")

    def test_rejects_colon_inside_part(self):
        with pytest.raises(ValueError):
            idempotency_key("feed:back", "2026-09-10")

    def test_same_period_same_key_different_run(self):
        # Dos "ejecuciones" del mismo periodo (no misma hora de ejecución)
        # deben producir la MISMA clave — la idempotencia depende del
        # periodo, no de cuándo corrió el job.
        k1 = idempotency_key("feedback", "2026-09-10")
        k2 = idempotency_key("feedback", "2026-09-10")
        assert k1 == k2
