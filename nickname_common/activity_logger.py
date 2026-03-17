"""
Activity Logger — escribe entradas al decisions_log.md de forma centralizada.

Cada agente puede importar esto para registrar decisiones automaticamente
en vez de escribir manualmente al log.

Uso:
    from nickname_common.activity_logger import ActivityLogger

    logger = ActivityLogger()  # auto-detecta ruta a decisions_log.md
    # o
    logger = ActivityLogger("/path/to/decisions_log.md")

    logger.log(
        task_id="T-ABC123",
        agent="CTO",
        system="nickname-email-marketing-agent",
        flow="Flujo 4",
        event="Deploy completado exitosamente",
        decision="DONE — verificado con curl",
        next_step="Ninguno",
    )
"""

import fcntl
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# Prefijos validos para task_id ademas del patron T-XXXXXX
_KNOWN_PREFIXES = {
    "HARDENING",
    "AUDIT",
    "FASE",
    "FLUJO",
    "POST-MORTEM",
    "INFRA",
    "UNKNOWN",
}

# Patron T-XXXXXX (al menos 4 caracteres hex/alfanumericos despues del guion)
_TASK_ID_PATTERN = re.compile(r"^T-[A-Za-z0-9]{4,}$")


def _is_valid_task_id(task_id: str) -> bool:
    """Valida que el task_id siga un patron conocido."""
    if _TASK_ID_PATTERN.match(task_id):
        return True
    # Comprobar prefijos conocidos (HARDENING, AUDIT-15MAR, FASE-1, etc.)
    for prefix in _KNOWN_PREFIXES:
        if task_id.startswith(prefix):
            return True
    return False


def _escape_pipes(text: str) -> str:
    """Escapa caracteres | en texto para evitar romper el formato del log."""
    return text.replace("|", "\\|")


def _find_decisions_log() -> Optional[Path]:
    """
    Busca decisions_log.md siguiendo esta prioridad:
    1. Variable de entorno AG_DECISIONS_LOG
    2. Subir desde CWD buscando .ag/decisions_log.md
    3. Fallback: ~/Desktop/Apps/.ag/decisions_log.md
    """
    # 1. Variable de entorno
    env_path = os.environ.get("AG_DECISIONS_LOG")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        _log.warning("AG_DECISIONS_LOG=%s no existe, buscando alternativas", env_path)

    # 2. Subir desde CWD
    current = Path.cwd()
    for directory in [current, *current.parents]:
        candidate = directory / ".ag" / "decisions_log.md"
        if candidate.is_file():
            return candidate

    # 3. Fallback
    fallback = Path.home() / "Desktop" / "Apps" / ".ag" / "decisions_log.md"
    if fallback.is_file():
        return fallback

    return None


class ActivityLogger:
    """Logger thread-safe y process-safe para decisions_log.md."""

    def __init__(self, log_path: Optional[str] = None):
        if log_path:
            self._path = Path(log_path)
        else:
            detected = _find_decisions_log()
            if detected is None:
                raise FileNotFoundError(
                    "No se encontro decisions_log.md. "
                    "Usa AG_DECISIONS_LOG env var o pasa la ruta explicitamente."
                )
            self._path = detected

        self._thread_lock = threading.Lock()

    @property
    def log_path(self) -> Path:
        return self._path

    def log(
        self,
        task_id: str,
        agent: str,
        system: str,
        flow: str,
        event: str,
        decision: str,
        next_step: str = "Ninguno",
    ) -> str:
        """
        Escribe una entrada completa al decisions_log.md.

        Retorna la linea escrita (sin newline final).
        """
        # Validar task_id
        if not _is_valid_task_id(task_id):
            _log.warning(
                "task_id '%s' no sigue el patron T-XXXXXX ni un prefijo conocido. "
                "Se escribira igualmente.",
                task_id,
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Escapar pipes en todos los campos de texto libre
        parts = [
            f"[{timestamp}]",
            _escape_pipes(task_id),
            _escape_pipes(agent),
            _escape_pipes(system),
            _escape_pipes(flow),
            _escape_pipes(event),
            _escape_pipes(decision),
            _escape_pipes(next_step),
        ]
        line = " | ".join(parts)

        self._append(line)
        return line

    def log_quick(
        self,
        agent: str,
        message: str,
        task_id: str = "QUICK",
        system: str = "—",
        flow: str = "—",
    ) -> str:
        """
        Atajo para entradas simples donde solo importan agente y mensaje.

        El mensaje se usa como event y decision se pone a '—'.
        """
        return self.log(
            task_id=task_id,
            agent=agent,
            system=system,
            flow=flow,
            event=message,
            decision="—",
            next_step="Ninguno",
        )

    def _append(self, line: str) -> None:
        """Escribe una linea al archivo con file locking y thread locking."""
        with self._thread_lock:
            with open(self._path, "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line + "\n")
                    f.flush()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
