"""Tests para nickname_common.health"""

from nickname_common.health import DeepHealthChecker


def test_checker_basic():
    checker = DeepHealthChecker(service_name="test", version="1.0.0")
    result = checker.run()
    assert result["service"] == "test"
    assert result["version"] == "1.0.0"
    assert result["status"] == "online"  # Sin checks = online


def test_checker_with_passing_check():
    checker = DeepHealthChecker(service_name="test", version="0.1")
    checker.add_check("db", lambda: "connected", timeout_sec=2)
    result = checker.run()
    assert result["status"] == "online"
    assert "db" in result["checks"]
    assert result["checks"]["db"]["status"] == "ok"


def test_checker_with_failing_check():
    checker = DeepHealthChecker(service_name="test", version="0.1")

    def _fail():
        raise ConnectionError("timeout")

    checker.add_check("db", _fail, timeout_sec=2)
    result = checker.run()
    assert result["status"] == "offline"  # Todos los checks fallaron = offline
    assert result["checks"]["db"]["status"] == "error"


def test_checker_mixed():
    """Un check OK + un check failed = degraded."""
    checker = DeepHealthChecker(service_name="test", version="0.1")
    checker.add_check("good", lambda: "ok", timeout_sec=2)

    def _bad():
        raise RuntimeError("fail")

    checker.add_check("bad", _bad, timeout_sec=2)
    result = checker.run()
    assert result["status"] == "degraded"
