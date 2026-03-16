"""
Modelo de AutomationJob — contrato para automatizaciones de governance.yaml.

Uso:
    from nickname_common.models import AutomationJob, AutomationSeverity, AutomationsResponse

    job = AutomationJob(
        name="flujo2_hubspot_odoo_sync",
        service="odoo-sync",
        frequency="10min",
        severity=AutomationSeverity.HIGH,
    )
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AutomationSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AutomationStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class AutomationJob:
    """Automatización registrada en governance.yaml."""
    name: str
    service: str
    frequency: str
    severity: AutomationSeverity = AutomationSeverity.MEDIUM
    status: AutomationStatus = AutomationStatus.ACTIVE
    description: str = ""
    last_run: str = ""
    autonomous: bool = True

    def __post_init__(self):
        if isinstance(self.severity, str):
            self.severity = AutomationSeverity(self.severity)
        if isinstance(self.status, str):
            self.status = AutomationStatus(self.status)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "service": self.service,
            "frequency": self.frequency,
            "severity": self.severity.value,
            "status": self.status.value,
            "description": self.description,
            "lastRun": self.last_run,
            "autonomous": self.autonomous,
        }


@dataclass
class AutomationsResponse:
    """Respuesta estándar de /api/ag/automations."""
    automations: list[AutomationJob] = field(default_factory=list)
    total: int = 0
    by_service: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if not self.total:
            self.total = len(self.automations)
        if not self.by_service:
            counts: dict[str, int] = {}
            for a in self.automations:
                counts[a.service] = counts.get(a.service, 0) + 1
            self.by_service = counts

    def to_dict(self) -> dict:
        return {
            "automations": [a.to_dict() for a in self.automations],
            "total": self.total,
            "byService": self.by_service,
        }
