"""Tests de BudgetGuard (WI-1, SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911 §4.3).

Cubre las cuatro categorías que pide la SPEC para esta pieza: presupuesto
(corte mensual + auditoría por llamada), concurrencia (el guard no introduce
su propia carrera sobre un store correctamente serializado), ausencia de
métricas (cost_micros=None nunca se convierte en 0 gastado) y fail-closed
(un store que no puede informar el gasto BLOQUEA, no autoriza).
"""

from __future__ import annotations

import threading

import pytest

from nickname_common.budget import (
    BudgetBlockedReason,
    BudgetExhausted,
    BudgetGuard,
    BudgetStatus,
)


class _MemoryStore:
    """Store de prueba: gasto acumulado en memoria, sin persistencia real."""

    def __init__(self, used_micros: int = 0, lock: threading.Lock | None = None):
        self._used = used_micros
        self._lock = lock

    def get_month_used_micros(self) -> int:
        return self._used

    def add_spent_micros(self, amount_micros: int) -> None:
        if self._lock is not None:
            with self._lock:
                self._used += amount_micros
        else:
            self._used += amount_micros


class _BrokenStore:
    """Store cuya lectura falla siempre — simula Volume/DB no disponible."""

    def get_month_used_micros(self) -> int:
        raise OSError("volumen no montado")

    def add_spent_micros(self, amount_micros: int) -> None:
        raise OSError("volumen no montado")


class _NegativeStore:
    """Store corrupto: devuelve un gasto negativo (imposible, debe bloquear)."""

    def get_month_used_micros(self) -> int:
        return -1

    def add_spent_micros(self, amount_micros: int) -> None:
        pass


# --- Construcción -------------------------------------------------------


def test_month_limit_must_be_positive():
    with pytest.raises(ValueError):
        BudgetGuard(store=_MemoryStore(), month_limit_micros=0)
    with pytest.raises(ValueError):
        BudgetGuard(store=_MemoryStore(), month_limit_micros=-1)


def test_per_call_limit_must_be_positive_if_set():
    with pytest.raises(ValueError):
        BudgetGuard(
            store=_MemoryStore(), month_limit_micros=100, per_call_limit_micros=0
        )


# --- Presupuesto: corte mensual ------------------------------------------


class TestPresupuestoMensual:
    def test_allows_call_under_budget(self):
        guard = BudgetGuard(store=_MemoryStore(used_micros=1_000_000), month_limit_micros=5_000_000)
        status = guard.check_before_call()
        assert status == BudgetStatus(1_000_000, 5_000_000, blocked=False)

    def test_blocks_exactly_at_limit(self):
        guard = BudgetGuard(store=_MemoryStore(used_micros=5_000_000), month_limit_micros=5_000_000)
        with pytest.raises(BudgetExhausted) as exc:
            guard.check_before_call()
        assert exc.value.reason is BudgetBlockedReason.MONTH_EXHAUSTED

    def test_blocks_over_limit(self):
        guard = BudgetGuard(store=_MemoryStore(used_micros=9_000_000), month_limit_micros=5_000_000)
        with pytest.raises(BudgetExhausted):
            guard.check_before_call()

    def test_status_reflects_blocked_without_raising(self):
        guard = BudgetGuard(store=_MemoryStore(used_micros=5_000_000), month_limit_micros=5_000_000)
        status = guard.status()
        assert status.blocked is True
        assert status.month_used_micros == 5_000_000
        assert status.store_error is None  # agotado de verdad, no store roto

    def test_status_to_dict_matches_health_contract(self):
        guard = BudgetGuard(store=_MemoryStore(used_micros=1), month_limit_micros=5_000_000)
        d = guard.status().to_dict()
        assert set(d) == {
            "budget.month_used_micros",
            "budget.month_limit_micros",
            "budget.blocked",
            "budget.readable",
        }
        assert d["budget.readable"] is True


# --- Presupuesto: auditoría por llamada ----------------------------------


class TestAuditoriaPorLlamada:
    def test_no_limit_configured_never_raises(self):
        guard = BudgetGuard(store=_MemoryStore(), month_limit_micros=5_000_000)
        guard.check_call_cost(1_000_000_000)  # no debe lanzar: no hay tope configurado

    def test_cost_within_per_call_limit_ok(self):
        guard = BudgetGuard(
            store=_MemoryStore(), month_limit_micros=5_000_000, per_call_limit_micros=100_000
        )
        guard.check_call_cost(50_000)  # no lanza

    def test_cost_over_per_call_limit_raises(self):
        guard = BudgetGuard(
            store=_MemoryStore(), month_limit_micros=5_000_000, per_call_limit_micros=100_000
        )
        with pytest.raises(BudgetExhausted) as exc:
            guard.check_call_cost(150_000)
        assert exc.value.reason is BudgetBlockedReason.PER_CALL_EXCEEDED

    def test_none_cost_never_audited(self):
        guard = BudgetGuard(
            store=_MemoryStore(), month_limit_micros=5_000_000, per_call_limit_micros=1
        )
        guard.check_call_cost(None)  # no lanza: no hay con qué auditar


# --- Ausencia de métricas: None nunca es 0 --------------------------------


class TestAusenciaDeMetricas:
    def test_record_call_with_none_does_not_add_spend(self):
        store = _MemoryStore(used_micros=1_000_000)
        guard = BudgetGuard(store=store, month_limit_micros=5_000_000)
        guard.record_call(None)
        assert store.get_month_used_micros() == 1_000_000, (
            "un coste desconocido NO debe sumarse como 0 — el gasto acumulado "
            "no debe moverse en absoluto"
        )

    def test_record_call_with_value_adds_exact_amount(self):
        store = _MemoryStore(used_micros=1_000_000)
        guard = BudgetGuard(store=store, month_limit_micros=5_000_000)
        guard.record_call(250_000)
        assert store.get_month_used_micros() == 1_250_000

    def test_record_call_rejects_negative(self):
        guard = BudgetGuard(store=_MemoryStore(), month_limit_micros=5_000_000)
        with pytest.raises(ValueError):
            guard.record_call(-1)


# --- Fail-closed: un store que no informa BLOQUEA, nunca autoriza --------


class TestFailClosed:
    def test_broken_store_blocks_before_call(self):
        guard = BudgetGuard(store=_BrokenStore(), month_limit_micros=5_000_000)
        with pytest.raises(BudgetExhausted) as exc:
            guard.check_before_call()
        assert exc.value.reason is BudgetBlockedReason.STORE_UNAVAILABLE

    def test_broken_store_status_is_blocked_not_an_exception(self):
        """El health del servicio debe poder responder 200 aunque el store
        de presupuesto esté roto (SPEC §5.3) — status() nunca propaga."""
        guard = BudgetGuard(store=_BrokenStore(), month_limit_micros=5_000_000)
        status = guard.status()
        assert status.blocked is True
        assert status.month_limit_micros == 5_000_000

    def test_broken_store_is_distinguishable_from_real_exhaustion(self):
        """blocked=True por sí solo no basta para diagnosticar: un store roto
        y un mes agotado de verdad exigen respuestas distintas."""
        broken = BudgetGuard(store=_BrokenStore(), month_limit_micros=5_000_000).status()
        exhausted = BudgetGuard(
            store=_MemoryStore(used_micros=5_000_000), month_limit_micros=5_000_000
        ).status()
        assert broken.blocked is True and exhausted.blocked is True
        assert broken.store_error is not None
        assert exhausted.store_error is None
        assert broken.to_dict()["budget.readable"] is False
        assert exhausted.to_dict()["budget.readable"] is True

    def test_negative_reported_spend_blocks(self):
        guard = BudgetGuard(store=_NegativeStore(), month_limit_micros=5_000_000)
        with pytest.raises(BudgetExhausted) as exc:
            guard.check_before_call()
        assert exc.value.reason is BudgetBlockedReason.STORE_UNAVAILABLE


# --- Concurrencia ---------------------------------------------------------


class TestConcurrencia:
    def test_concurrent_record_calls_with_locked_store_sum_exactly(self):
        """El guard no debe introducir SU PROPIA carrera: si el store que le
        da el consumidor está correctamente serializado (aquí, un Lock),
        N llamadas concurrentes de coste fijo deben sumar exactamente
        N * coste — cero updates perdidos."""
        lock = threading.Lock()
        store = _MemoryStore(used_micros=0, lock=lock)
        guard = BudgetGuard(store=store, month_limit_micros=1_000_000_000)

        hilos = [
            threading.Thread(target=guard.record_call, args=(1_000,))
            for _ in range(50)
        ]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        assert store.get_month_used_micros() == 50_000

    def test_concurrent_none_costs_never_move_spend(self):
        lock = threading.Lock()
        store = _MemoryStore(used_micros=500, lock=lock)
        guard = BudgetGuard(store=store, month_limit_micros=1_000_000_000)

        hilos = [
            threading.Thread(target=guard.record_call, args=(None,))
            for _ in range(20)
        ]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        assert store.get_month_used_micros() == 500
