"""
BudgetGuard — corte de gasto LLM determinista y fail-closed, por servicio/job.

Nace de SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911 §4.3: presupuesto mensual
conjunto inicial de 5,00 USD y tope de 0,10 USD por llamada para los jobs
Railway nuevos. "Al llegar al límite, conserva el resultado determinista y
omite el LLM" — este módulo es exactamente esa decisión: SÍ / NO se llama al
modelo, nunca decide si la llamada hace falta (eso lo decide el job con su
propia huella/delta, ver `nickname_common.fingerprint`).

Diseño deliberado:
  - Cero dependencias externas (regla del repo, ver CLAUDE.md): el estado de
    gasto acumulado lo guarda el CONSUMIDOR (Railway Volume, tabla, lo que
    tenga) detrás de un `BudgetStore` — este módulo no sabe de ficheros ni
    de bases de datos, solo de la decisión aritmética.
  - Fail-closed: si el store no puede informar con certeza el gasto
    acumulado del mes (excepción, valor negativo), el guard NIEGA la
    llamada. Nunca asume "presupuesto disponible" por no poder comprobarlo
    — ver criterios globales de la SPEC: "ausencia de métricas es null,
    nunca cero inventado" se traduce aquí en "ausencia de certeza de gasto
    bloquea, nunca autoriza".
  - Concurrencia: este módulo no serializa nada por sí mismo (stdlib puro,
    sin locks de proceso). Si dos procesos del mismo servicio pueden llamar
    `record_call()` a la vez, el `BudgetStore` que implemente el consumidor
    debe serializar su propia escritura (lock de fichero, transacción
    atómica de DB, etc.) — este guard solo compone lecturas/escrituras que
    el store le entrega.
  - Idempotencia: este guard NO deduplica llamadas por sí mismo. Un
    consumidor que reintenta un run debe comprobar su `idempotency_key`
    ANTES de llamar a `record_call()` de nuevo — sumar el mismo coste dos
    veces por un retry es un bug del consumidor, no algo que este módulo
    pueda adivinar sin conocer el dominio del job.

Uso típico:
    guard = BudgetGuard(
        store=mi_store_railway_volume,
        month_limit_micros=5_000_000,     # 5,00 USD
        per_call_limit_micros=100_000,    # 0,10 USD
    )
    guard.check_before_call()             # BudgetExhausted si no hay margen
    resultado = nickname_common.llm.complete(...)
    cost = resultado.usage.get("cost_micros")   # puede ser None
    guard.check_call_cost(cost)           # audita post-hoc; no impide lo ya ocurrido
    guard.record_call(cost)               # None no suma (no inventa 0)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class BudgetBlockedReason(str, Enum):
    MONTH_EXHAUSTED = "month_budget_exhausted"
    PER_CALL_EXCEEDED = "per_call_cap_exceeded"
    STORE_UNAVAILABLE = "budget_store_unavailable"


class BudgetExhausted(RuntimeError):
    """Presupuesto agotado o no verificable con certeza: fail-closed.

    El consumidor debe capturar esta excepción, conservar su parte
    determinista y marcar el resultado como `llm_skipped=budget_exhausted`
    (o el motivo correspondiente) — NUNCA reintentar la llamada a ciegas.
    """

    def __init__(self, reason: BudgetBlockedReason, detail: str = ""):
        self.reason = reason
        self.detail = detail
        message = reason.value if not detail else f"{reason.value}: {detail}"
        super().__init__(message)


@runtime_checkable
class BudgetStore(Protocol):
    """Contrato que implementa el consumidor (Railway Volume, tabla, etc.).

    `get_month_used_micros()` debe devolver el gasto acumulado del mes en
    curso en micro-USD, o lanzar cualquier excepción si no puede leerlo con
    certeza. El guard trata CUALQUIER excepción de este método como
    fail-closed — nunca como "0 gastado este mes".
    """

    def get_month_used_micros(self) -> int: ...

    def add_spent_micros(self, amount_micros: int) -> None: ...


@dataclass(frozen=True)
class BudgetStatus:
    """Forma exacta que espera el health del servicio (SPEC §5.3), más
    `store_error` para distinguir presupuesto agotado de store no legible."""

    month_used_micros: int
    month_limit_micros: int
    blocked: bool
    store_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "budget.month_used_micros": self.month_used_micros,
            "budget.month_limit_micros": self.month_limit_micros,
            "budget.blocked": self.blocked,
            "budget.readable": self.store_error is None,
        }


class BudgetGuard:
    """Corte de gasto mensual (fail-closed) + auditoría de tope por llamada.

    El tope mensual es PREVENTIVO: `check_before_call()` se llama ANTES de
    invocar al modelo y bloquea si no hay margen.

    El tope por llamada es AUDITORÍA POST-HOC, no prevención: el coste real
    de una llamada solo se conoce cuando el proveedor responde, así que
    `check_call_cost()` se llama DESPUÉS de la respuesta. Si el coste real
    supera el tope, la llamada ya ocurrió — la excepción sirve para que el
    consumidor lo trate como anomalía a revisar (alerta, dejar de programar
    más llamadas ese ciclo), no para "deshacer" un gasto que ya sucedió. El
    control realmente preventivo de coste-por-llamada es acotar
    `max_tokens`/payload antes de llamar (ver SPEC §4.3: payload máximo
    64 KiB, output máximo 1.200 tokens) — responsabilidad del consumidor,
    no de este guard.
    """

    def __init__(
        self,
        *,
        store: BudgetStore,
        month_limit_micros: int,
        per_call_limit_micros: int | None = None,
    ) -> None:
        if month_limit_micros <= 0:
            raise ValueError("month_limit_micros debe ser > 0")
        if per_call_limit_micros is not None and per_call_limit_micros <= 0:
            raise ValueError("per_call_limit_micros debe ser > 0 si se define")
        self._store = store
        self._month_limit_micros = month_limit_micros
        self._per_call_limit_micros = per_call_limit_micros

    @property
    def month_limit_micros(self) -> int:
        return self._month_limit_micros

    @property
    def per_call_limit_micros(self) -> int | None:
        return self._per_call_limit_micros

    def _safe_used_micros(self) -> int:
        try:
            used = self._store.get_month_used_micros()
        except Exception as exc:  # noqa: BLE001 — fail-closed deliberado y documentado
            raise BudgetExhausted(
                BudgetBlockedReason.STORE_UNAVAILABLE,
                f"{type(exc).__name__} leyendo el gasto acumulado: no se asume 0",
            ) from exc
        if used < 0:
            raise BudgetExhausted(
                BudgetBlockedReason.STORE_UNAVAILABLE,
                f"gasto acumulado negativo ({used}) — store corrupto",
            )
        return used

    def status(self) -> BudgetStatus:
        """Estado para el health del servicio. NO lanza: un store roto se
        refleja como `blocked=True` con el límite conocido, para que el
        endpoint de salud siga respondiendo (ver SPEC §5.3: "Un health=200
        acredita proceso y contrato técnico", no que haya presupuesto).

        `store_error` distingue las dos causas de `blocked=True` que un
        consumidor NO debe tratar igual: presupuesto agotado de verdad
        (`store_error=None`, hay certeza del gasto) frente a "no se pudo leer
        el gasto con certeza" (`store_error` con el motivo). Colapsar ambas
        en un solo booleano escondería justo el caso — log corrupto, volumen
        no montado — que más urge diagnosticar distinto de un simple "se
        acabó el mes"."""
        try:
            used = self._safe_used_micros()
        except BudgetExhausted as exc:
            return BudgetStatus(
                month_used_micros=self._month_limit_micros,
                month_limit_micros=self._month_limit_micros,
                blocked=True,
                store_error=str(exc),
            )
        return BudgetStatus(
            month_used_micros=used,
            month_limit_micros=self._month_limit_micros,
            blocked=used >= self._month_limit_micros,
        )

    def check_before_call(self) -> BudgetStatus:
        """Levanta `BudgetExhausted` si no hay margen mensual. No decide POR
        SÍ SOLO si la llamada hace falta — esa decisión (huella/delta) es del
        job, anterior a esta comprobación."""
        used = self._safe_used_micros()
        if used >= self._month_limit_micros:
            raise BudgetExhausted(
                BudgetBlockedReason.MONTH_EXHAUSTED,
                f"{used} >= {self._month_limit_micros} micro-USD del mes en curso",
            )
        return BudgetStatus(used, self._month_limit_micros, blocked=False)

    def check_call_cost(self, cost_micros: int | None) -> None:
        """Audita el coste YA CONOCIDO de una llamada ya ocurrida contra el
        tope por llamada. `cost_micros=None` (proveedor no informó coste) o
        sin tope configurado: no hay nada que auditar, no levanta nada."""
        if cost_micros is None or self._per_call_limit_micros is None:
            return
        if cost_micros > self._per_call_limit_micros:
            raise BudgetExhausted(
                BudgetBlockedReason.PER_CALL_EXCEEDED,
                f"{cost_micros} > {self._per_call_limit_micros} micro-USD en una sola llamada",
            )

    def record_call(self, cost_micros: int | None) -> None:
        """Suma el coste real al store. `cost_micros=None` no suma nada — un
        coste desconocido NUNCA se registra como 0 gastado."""
        if cost_micros is None:
            return
        if cost_micros < 0:
            raise ValueError(f"cost_micros no puede ser negativo: {cost_micros}")
        self._store.add_spent_micros(cost_micros)
