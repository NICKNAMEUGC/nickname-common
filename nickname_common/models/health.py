"""
Modelo de HealthResponse — contrato estándar para /api/health en todos los agentes.

Cada agente DEBE devolver este formato en /api/health para que el dashboard
y los health checks funcionen sin if/else por agente.

Uso:
    from nickname_common.models import HealthResponse, HealthCheck, ServiceStatus

    # Validar respuesta de un agente
    response = HealthResponse(**data)

    # Crear respuesta en un agente
    response = HealthResponse(
        service="email-marketing",
        version="1.2.0",
        status=ServiceStatus.ONLINE,
        checks=[
            HealthCheck(name="odoo", status="ok", latency_ms=142),
        ],
    )
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ServiceStatus(str, Enum):
    """Estado global del servicio."""
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass
class HealthCheck:
    """Resultado de un check individual (odoo, hubspot, etc.)."""
    name: str
    status: str  # "ok" | "error" | "timeout"
    latency_ms: int = 0
    detail: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["detail"] is None:
            del d["detail"]
        return d


@dataclass
class HealthResponse:
    """Respuesta estándar de /api/health.

    Contrato:
    - service: nombre del servicio (ej: "nickname-super-agent")
    - version: versión del servicio
    - status: "online" | "degraded" | "offline"
    - timestamp: ISO 8601 UTC
    - checks: lista de checks individuales (puede estar vacía)
    """
    service: str
    version: str = "0.0.0"
    status: ServiceStatus = ServiceStatus.ONLINE
    timestamp: str = ""
    checks: list[HealthCheck] = field(default_factory=list)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if isinstance(self.status, str):
            self.status = ServiceStatus(self.status)

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "version": self.version,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "checks": {c.name: c.to_dict() for c in self.checks},
        }

    @classmethod
    def from_deep_health_checker(cls, result: dict) -> "HealthResponse":
        """Convierte el output de DeepHealthChecker.run() a HealthResponse."""
        checks = []
        for name, data in result.get("checks", {}).items():
            checks.append(HealthCheck(
                name=name,
                status=data.get("status", "error"),
                latency_ms=data.get("latency_ms", 0),
                detail=data.get("detail"),
            ))
        return cls(
            service=result.get("service", "unknown"),
            version=result.get("version", "0.0.0"),
            status=ServiceStatus(result.get("status", "offline")),
            timestamp=result.get("timestamp", ""),
            checks=checks,
        )
