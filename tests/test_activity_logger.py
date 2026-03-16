"""Tests para nickname_common.activity_logger."""

import os
import re
import tempfile
import threading

import pytest

from nickname_common.activity_logger import ActivityLogger, _is_valid_task_id


@pytest.fixture
def tmp_log(tmp_path):
    """Crea un decisions_log.md temporal con cabecera minima."""
    log_file = tmp_path / "decisions_log.md"
    log_file.write_text("# decisions_log.md\n\n## Entradas\n\n", encoding="utf-8")
    return str(log_file)


class TestLog:
    def test_log_creates_entry(self, tmp_log):
        """Verifica que log() escribe una entrada con el formato correcto."""
        logger = ActivityLogger(tmp_log)
        line = logger.log(
            task_id="T-ABC123",
            agent="CTO",
            system="nickname-super-agent",
            flow="Flujo 4",
            event="Deploy completado",
            decision="DONE",
            next_step="Ninguno",
        )

        content = open(tmp_log, encoding="utf-8").read()
        assert line in content

        # Verificar que tiene 8 campos separados por |
        parts = line.split(" | ")
        assert len(parts) == 8

        # Verificar campos
        assert parts[1] == "T-ABC123"
        assert parts[2] == "CTO"
        assert parts[3] == "nickname-super-agent"
        assert parts[4] == "Flujo 4"
        assert parts[5] == "Deploy completado"
        assert parts[6] == "DONE"
        assert parts[7] == "Ninguno"

    def test_auto_timestamp(self, tmp_log):
        """Verifica que el timestamp es ISO 8601 UTC."""
        logger = ActivityLogger(tmp_log)
        line = logger.log(
            task_id="T-TEST01",
            agent="Test",
            system="test",
            flow="—",
            event="test event",
            decision="test decision",
        )

        # Extraer timestamp del formato [YYYY-MM-DDTHH:MM:SSZ]
        match = re.match(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\]", line)
        assert match is not None, f"Timestamp no encontrado en: {line}"

        # Verificar que termina en Z (UTC)
        ts = match.group(1)
        assert ts.endswith("Z")

    def test_log_quick(self, tmp_log):
        """Verifica que log_quick() funciona como atajo."""
        logger = ActivityLogger(tmp_log)
        line = logger.log_quick(
            agent="AI Ops Manager",
            message="Health check OK",
        )

        parts = line.split(" | ")
        assert len(parts) == 8
        assert parts[1] == "QUICK"
        assert parts[2] == "AI Ops Manager"
        assert parts[5] == "Health check OK"
        assert parts[6] == "—"
        assert parts[7] == "Ninguno"

    def test_log_quick_with_custom_task_id(self, tmp_log):
        """Verifica que log_quick() acepta task_id custom."""
        logger = ActivityLogger(tmp_log)
        line = logger.log_quick(
            agent="CTO",
            message="Diagnostico rapido",
            task_id="T-DIAG01",
            system="nickname-fiscal-agent",
            flow="Flujo 4",
        )

        parts = line.split(" | ")
        assert parts[1] == "T-DIAG01"
        assert parts[3] == "nickname-fiscal-agent"
        assert parts[4] == "Flujo 4"


class TestThreadSafety:
    def test_concurrent_writes(self, tmp_log):
        """10 threads escriben simultaneamente, todos deben estar presentes."""
        logger = ActivityLogger(tmp_log)
        errors = []

        def write_entry(i):
            try:
                logger.log(
                    task_id=f"T-THREAD{i:02d}",
                    agent=f"Agent-{i}",
                    system="test",
                    flow="—",
                    event=f"Concurrent write {i}",
                    decision="OK",
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_entry, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errores durante escritura concurrente: {errors}"

        content = open(tmp_log, encoding="utf-8").read()
        for i in range(10):
            assert f"T-THREAD{i:02d}" in content, f"Falta entrada T-THREAD{i:02d}"


class TestValidation:
    def test_valid_task_ids(self):
        """Task IDs validos no deben generar warnings."""
        assert _is_valid_task_id("T-ABC123")
        assert _is_valid_task_id("T-F4D7AC")
        assert _is_valid_task_id("T-EMKTG01")
        assert _is_valid_task_id("HARDENING")
        assert _is_valid_task_id("AUDIT-15MAR")
        assert _is_valid_task_id("FASE-1")
        assert _is_valid_task_id("POST-MORTEM")
        assert _is_valid_task_id("INFRA-MINI")

    def test_invalid_task_id_warning(self, tmp_log, caplog):
        """Un task_id invalido genera warning pero no crash."""
        logger = ActivityLogger(tmp_log)

        import logging
        with caplog.at_level(logging.WARNING, logger="nickname_common.activity_logger"):
            line = logger.log(
                task_id="bad-id",
                agent="Test",
                system="test",
                flow="—",
                event="test",
                decision="test",
            )

        # No debe haber crasheado
        assert line is not None
        assert "bad-id" in line

        # Debe haber loggeado un warning
        assert any("bad-id" in record.message for record in caplog.records)

    def test_pipe_chars_escaped(self, tmp_log):
        """Si un campo contiene |, debe escaparse a \\| para no romper el formato."""
        logger = ActivityLogger(tmp_log)
        line = logger.log(
            task_id="T-PIPE01",
            agent="CTO",
            system="test",
            flow="Flujo 4",
            event="Error: timeout | connection refused",
            decision="Reintentar | escalar si falla",
            next_step="Ninguno",
        )

        # Los | dentro de campos deben estar escapados
        assert "timeout \\| connection refused" in line
        assert "Reintentar \\| escalar si falla" in line

        # Verificar que al splitear por " | " (sin escape) obtenemos 8 campos
        # Los \| no deben romper el split
        parts = line.split(" | ")
        assert len(parts) == 8


class TestAutoDetect:
    def test_env_var_override(self, tmp_log):
        """AG_DECISIONS_LOG env var debe tener prioridad."""
        os.environ["AG_DECISIONS_LOG"] = tmp_log
        try:
            logger = ActivityLogger()
            assert str(logger.log_path) == tmp_log
        finally:
            del os.environ["AG_DECISIONS_LOG"]

    def test_explicit_path(self, tmp_log):
        """Una ruta explicita debe usarse directamente."""
        logger = ActivityLogger(tmp_log)
        assert str(logger.log_path) == tmp_log

    def test_missing_file_raises(self, monkeypatch):
        """Si auto-detect no encuentra el archivo, debe lanzar FileNotFoundError."""
        # Limpiar env var y forzar CWD a /tmp para que no encuentre .ag/
        monkeypatch.delenv("AG_DECISIONS_LOG", raising=False)
        monkeypatch.chdir("/tmp")

        # Parchear el fallback para que tampoco exista
        import nickname_common.activity_logger as mod
        original_find = mod._find_decisions_log

        def _mock_find():
            return None

        monkeypatch.setattr(mod, "_find_decisions_log", _mock_find)

        with pytest.raises(FileNotFoundError):
            ActivityLogger()


class TestAppendOnly:
    def test_existing_content_preserved(self, tmp_log):
        """Las entradas existentes no deben modificarse."""
        original = open(tmp_log, encoding="utf-8").read()

        logger = ActivityLogger(tmp_log)
        logger.log(
            task_id="T-NEW001",
            agent="Test",
            system="test",
            flow="—",
            event="Nueva entrada",
            decision="OK",
        )

        content = open(tmp_log, encoding="utf-8").read()
        assert content.startswith(original)
        assert "T-NEW001" in content
