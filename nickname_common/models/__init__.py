"""
nickname_common.models — Modelos compartidos (contratos) para todos los agentes.

Equivalente a packages/shared/validators de Paperclip.
Centraliza los schemas para que dashboard y agentes hablen el mismo idioma.
"""

from nickname_common.models.health import (
    HealthCheck,
    HealthResponse,
    ServiceStatus,
)
from nickname_common.models.task import (
    Task,
    TaskStatus,
    TaskPriority,
    TasksResponse,
)
from nickname_common.models.activity import (
    ActivityEntry,
    ActivityLevel,
)
from nickname_common.models.automation import (
    AutomationJob,
    AutomationSeverity,
    AutomationsResponse,
)
