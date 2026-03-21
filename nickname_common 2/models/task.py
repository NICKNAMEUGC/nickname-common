"""
Modelo de Task — contrato estándar para tareas en active_tasks.md.

Uso:
    from nickname_common.models import Task, TaskStatus, TasksResponse

    task = Task(
        id="T-ABC123",
        title="Migrar templates email",
        status=TaskStatus.IN_PROGRESS,
        agent="email_social_specialist",
    )
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PENDING_CC = "pending_cc"  # Pendiente de Claude Code
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    STALE = "stale"


class TaskPriority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Task:
    """Tarea del sistema operativo Antigravity."""
    id: str
    title: str
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    agent: str = ""
    system: str = ""
    flow: str = ""
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
    ttl_hours: int = 24
    blocked_by: str = ""
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if isinstance(self.status, str):
            self.status = TaskStatus(self.status)
        if isinstance(self.priority, str):
            self.priority = TaskPriority(self.priority)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "priority": self.priority.value,
            "assignee": self.agent,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "description": self.description,
            "tags": self.tags,
        }

    @property
    def is_active(self) -> bool:
        return self.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.PENDING_CC)


@dataclass
class TasksResponse:
    """Respuesta estándar de /api/ag/tasks."""
    tasks: list[Task] = field(default_factory=list)
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if not self.total:
            self.total = len(self.tasks)
        if not self.by_status:
            counts: dict[str, int] = {}
            for t in self.tasks:
                s = t.status.value if isinstance(t.status, TaskStatus) else t.status
                counts[s] = counts.get(s, 0) + 1
            self.by_status = counts

    def to_dict(self) -> dict:
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "total": self.total,
            "byStatus": self.by_status,
        }
