"""Tests para nickname_common.models — contratos compartidos."""

from nickname_common.models import (
    HealthCheck, HealthResponse, ServiceStatus,
    Task, TaskStatus, TaskPriority, TasksResponse,
    ActivityEntry, ActivityLevel,
    AutomationJob, AutomationSeverity, AutomationsResponse,
)


# --- HealthCheck / HealthResponse ---

def test_health_check_to_dict():
    hc = HealthCheck(name="odoo", status="ok", latency_ms=42)
    d = hc.to_dict()
    assert d["name"] == "odoo"
    assert d["latency_ms"] == 42
    assert "detail" not in d  # None se omite


def test_health_response_to_dict():
    hr = HealthResponse(
        service="super-agent",
        version="2.0.0",
        status=ServiceStatus.ONLINE,
        checks=[HealthCheck(name="db", status="ok", latency_ms=10)],
    )
    d = hr.to_dict()
    assert d["status"] == "online"
    assert "db" in d["checks"]


def test_health_response_string_coercion():
    hr = HealthResponse(service="test", status="degraded")
    assert hr.status == ServiceStatus.DEGRADED


def test_health_response_from_deep_checker():
    raw = {
        "service": "email-marketing",
        "version": "1.0.0",
        "status": "online",
        "timestamp": "2026-03-16T10:00:00Z",
        "checks": {
            "odoo": {"status": "ok", "latency_ms": 50},
            "redis": {"status": "error", "latency_ms": 0, "detail": "timeout"},
        },
    }
    hr = HealthResponse.from_deep_health_checker(raw)
    assert hr.service == "email-marketing"
    assert len(hr.checks) == 2


# --- Task / TasksResponse ---

def test_task_defaults():
    t = Task(id="T-001", title="Test")
    assert t.status == TaskStatus.PENDING
    assert t.priority == TaskPriority.MEDIUM
    assert t.created_at  # auto-generated


def test_task_string_coercion():
    t = Task(id="T-002", title="X", status="completed", priority="high")
    assert t.status == TaskStatus.COMPLETED
    assert t.priority == TaskPriority.HIGH


def test_task_is_active():
    assert Task(id="1", title="a", status=TaskStatus.PENDING).is_active
    assert Task(id="2", title="b", status=TaskStatus.IN_PROGRESS).is_active
    assert Task(id="3", title="c", status=TaskStatus.PENDING_CC).is_active
    assert not Task(id="4", title="d", status=TaskStatus.COMPLETED).is_active
    assert not Task(id="5", title="e", status=TaskStatus.BLOCKED).is_active


def test_task_to_dict():
    t = Task(id="T-010", title="Deploy", agent="cto", status=TaskStatus.IN_PROGRESS)
    d = t.to_dict()
    assert d["id"] == "T-010"
    assert d["status"] == "in_progress"
    assert d["assignee"] == "cto"


def test_tasks_response_auto_counts():
    tasks = [
        Task(id="1", title="a", status=TaskStatus.PENDING),
        Task(id="2", title="b", status=TaskStatus.PENDING),
        Task(id="3", title="c", status=TaskStatus.COMPLETED),
    ]
    resp = TasksResponse(tasks=tasks)
    assert resp.total == 3
    assert resp.by_status["pending"] == 2
    assert resp.by_status["completed"] == 1


# --- ActivityEntry ---

def test_activity_entry_to_dict():
    e = ActivityEntry(
        timestamp="2026-03-16T10:00:00Z",
        level=ActivityLevel.WARNING,
        agent="orchestrator",
        message="Sync retrasado",
    )
    d = e.to_dict()
    assert d["level"] == "warning"
    assert d["agent"] == "orchestrator"


def test_activity_entry_string_coercion():
    e = ActivityEntry(timestamp="", level="error", agent="x", message="fail")
    assert e.level == ActivityLevel.ERROR


# --- AutomationJob / AutomationsResponse ---

def test_automation_job_to_dict():
    j = AutomationJob(
        name="flujo2",
        service="odoo-sync",
        frequency="10min",
        severity=AutomationSeverity.HIGH,
    )
    d = j.to_dict()
    assert d["severity"] == "high"
    assert d["status"] == "active"
    assert d["autonomous"] is True


def test_automation_job_string_coercion():
    j = AutomationJob(name="x", service="y", frequency="5min", severity="critical", status="paused")
    assert j.severity == AutomationSeverity.CRITICAL
    from nickname_common.models.automation import AutomationStatus
    assert j.status == AutomationStatus.PAUSED


def test_automations_response_auto_counts():
    jobs = [
        AutomationJob(name="a", service="odoo", frequency="5min"),
        AutomationJob(name="b", service="odoo", frequency="10min"),
        AutomationJob(name="c", service="emktg", frequency="1h"),
    ]
    resp = AutomationsResponse(automations=jobs)
    assert resp.total == 3
    assert resp.by_service["odoo"] == 2
    assert resp.by_service["emktg"] == 1


def test_automations_response_to_dict():
    jobs = [AutomationJob(name="test", service="svc", frequency="1h")]
    resp = AutomationsResponse(automations=jobs)
    d = resp.to_dict()
    assert d["total"] == 1
    assert "svc" in d["byService"]
    assert len(d["automations"]) == 1
