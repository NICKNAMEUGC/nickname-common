"""
AutomationRunV1 y LLMUsageEventV1 — contrato de observabilidad para los jobs
recurrentes de SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911 (§5).

Cada ejecución de un job emite UN `AutomationRunV1` (qué corrió, si hubo
novedad, si hizo falta LLM) y CERO o MÁS `LLMUsageEventV1` (cada llamada real
al modelo, con su coste tipado). Ninguno de los dos lleva PII, prompts,
respuestas ni contenido de negocio — son metadatos de ejecución y gasto.

Uso:
    from nickname_common.models import (
        AutomationRunV1, RunStatus, SourceStatus,
        LLMUsageEventV1, LLMOutcome, CostSource,
    )

    run = AutomationRunV1(
        job_id="roomservice_feedback_daily",
        run_id=str(uuid.uuid4()),
        idempotency_key=idempotency_key("feedback", "2026-09-10"),
        source_status=SourceStatus.OK,
        changed=True,
        llm_required=True,
    )
    ... llamar al LLM si hace falta, emitir LLMUsageEventV1 ...
    run.llm_calls = 1
    run.result_ref = "report:2026-09-10"
    run.status = RunStatus.OK
    run.mark_completed()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class RunStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class SourceStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"


class LLMOutcome(str, Enum):
    SERVED = "served"
    TRANSPORT_ERROR = "transport_error"
    CONTENT_ERROR = "content_error"
    TIMEOUT = "timeout"
    BUDGET_BLOCKED = "budget_blocked"


class CostSource(str, Enum):
    PROVIDER_RESPONSE = "provider_response"
    CATALOG = "catalog"
    UNAVAILABLE = "unavailable"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AutomationRunV1:
    """Una ejecución de un job recurrente. Ver SPEC §5.1 para el JSON exacto."""

    job_id: str
    run_id: str
    idempotency_key: str
    started_at: str = field(default_factory=_utc_now_iso)
    completed_at: str = ""
    status: RunStatus = RunStatus.OK
    source_status: SourceStatus = SourceStatus.OK
    changed: bool = False
    llm_required: bool = False
    llm_calls: int = 0
    result_ref: Optional[str] = None
    error_code: Optional[str] = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = RunStatus(self.status)
        if isinstance(self.source_status, str):
            self.source_status = SourceStatus(self.source_status)
        if self.llm_calls < 0:
            raise ValueError("llm_calls no puede ser negativo")
        if not self.llm_required and self.llm_calls > 0:
            raise ValueError(
                "llm_calls > 0 con llm_required=False es inconsistente: "
                "si hubo llamadas, el run declaró que hacían falta"
            )

    def mark_completed(self) -> "AutomationRunV1":
        """Fija `completed_at` a ahora. El caller decide cuándo cerrar el
        run — este helper solo evita repetir `datetime.now(...)` en cada
        consumidor."""
        self.completed_at = _utc_now_iso()
        return self

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status.value,
            "source_status": self.source_status.value,
            "changed": self.changed,
            "llm_required": self.llm_required,
            "llm_calls": self.llm_calls,
            "result_ref": self.result_ref,
            "error_code": self.error_code,
        }


@dataclass
class LLMUsageEventV1:
    """Una llamada real (o bloqueada) al modelo. Ver SPEC §5.2.

    Invariante dura: `cost_source` y `cost_micros` deben ser consistentes.
    "Ausente no equivale a cero" (criterio global de la SPEC) se aplica aquí
    como una excepción en construcción, no como una convención que un
    consumidor pueda olvidar:
      - `cost_source=UNAVAILABLE`  → `cost_micros` DEBE ser None.
      - `cost_source` en {PROVIDER_RESPONSE, CATALOG} → `cost_micros` DEBE
        tener un valor (nunca None silencioso).
    """

    service: str
    job_id: str
    run_id: str
    reason: str
    provider: str
    requested_model: str
    outcome: LLMOutcome
    response_model: Optional[str] = None
    input_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_micros: Optional[int] = None
    cost_currency: str = "USD"
    cost_source: CostSource = CostSource.UNAVAILABLE
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.outcome, str):
            self.outcome = LLMOutcome(self.outcome)
        if isinstance(self.cost_source, str):
            self.cost_source = CostSource(self.cost_source)
        if self.cost_source is CostSource.UNAVAILABLE and self.cost_micros is not None:
            raise ValueError(
                "cost_source=unavailable exige cost_micros=None — "
                "no inventar un coste cuando no hay fuente para él"
            )
        if (
            self.cost_source in (CostSource.PROVIDER_RESPONSE, CostSource.CATALOG)
            and self.cost_micros is None
        ):
            raise ValueError(
                f"cost_source={self.cost_source.value} exige cost_micros con "
                "valor — no dejar el coste en None cuando sí hay fuente"
            )
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("cached_input_tokens", self.cached_input_tokens),
            ("output_tokens", self.output_tokens),
            ("cost_micros", self.cost_micros),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} no puede ser negativo: {value}")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "service": self.service,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "reason": self.reason,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "response_model": self.response_model,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "cost_micros": self.cost_micros,
            "cost_currency": self.cost_currency,
            "cost_source": self.cost_source.value,
            "outcome": self.outcome.value,
        }

    @classmethod
    def budget_blocked(
        cls,
        *,
        service: str,
        job_id: str,
        run_id: str,
        reason: str,
        requested_model: str,
    ) -> "LLMUsageEventV1":
        """Evento tipado para el caso "presupuesto agotado, se omite el LLM"
        (SPEC §4.3). `provider` se marca "none" porque la llamada nunca
        salió a red — es la forma tipada de "omitido", no un intento fallido."""
        return cls(
            service=service,
            job_id=job_id,
            run_id=run_id,
            reason=reason,
            provider="none",
            requested_model=requested_model,
            outcome=LLMOutcome.BUDGET_BLOCKED,
            cost_source=CostSource.UNAVAILABLE,
        )
