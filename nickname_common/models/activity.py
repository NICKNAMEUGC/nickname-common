"""
Modelo de ActivityEntry — contrato para entries del decisions_log.

Uso:
    from nickname_common.models import ActivityEntry, ActivityLevel

    entry = ActivityEntry(
        timestamp="2026-03-16T10:00:00Z",
        level=ActivityLevel.INFO,
        agent="orchestrator",
        message="Sync HubSpot completado",
    )
"""

from dataclasses import dataclass
from enum import Enum


class ActivityLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


@dataclass
class ActivityEntry:
    """Entrada del log de actividad (decisions_log.md)."""
    timestamp: str
    level: ActivityLevel
    agent: str
    message: str
    task_id: str = ""
    detail: str = ""

    def __post_init__(self):
        if isinstance(self.level, str):
            self.level = ActivityLevel(self.level)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "level": self.level.value,
            "agent": self.agent,
            "message": self.message,
            "taskId": self.task_id,
            "detail": self.detail,
        }
