"""
Logging estándar para servicios Nickname.

Uso:
    from nickname_common import setup_logger
    logger = setup_logger("email-marketing")
    logger.info("Worker iniciado")
    # → [EMAIL-MARKETING] [INFO] Worker iniciado

Respeta la variable de entorno LOG_LEVEL (default: INFO).
"""

import logging
import os
import sys


_configured_loggers: dict = {}


def setup_logger(service_name: str, level: str = None) -> logging.Logger:
    """Configura y devuelve un logger con formato estándar Nickname.

    Args:
        service_name: Nombre del servicio (se usa en mayúsculas en el prefijo)
        level: Nivel de log ('DEBUG', 'INFO', 'WARNING', 'ERROR').
               Si no se especifica, usa LOG_LEVEL env var o INFO por defecto.

    Returns:
        Logger configurado con handler de stdout.
    """
    # Reutilizar si ya existe
    key = service_name.lower()
    if key in _configured_loggers:
        return _configured_loggers[key]

    # Determinar nivel
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO").upper()

    numeric_level = getattr(logging, level, logging.INFO)

    # Crear logger
    logger = logging.getLogger(f"nickname.{key}")
    logger.setLevel(numeric_level)

    # Evitar duplicar handlers si se llama múltiples veces
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(numeric_level)

        prefix = service_name.upper().replace("-", "-")
        formatter = logging.Formatter(
            f"[{prefix}] [%(levelname)s] %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    # No propagar al root logger (evita duplicados)
    logger.propagate = False

    _configured_loggers[key] = logger
    return logger


def setup_logger_safe(service_name: str, level: str = None) -> logging.Logger:
    """Igual que setup_logger pero con RedactingFilter incluido.

    Redacta automáticamente API keys, tokens, emails y passwords de los logs.

    Args:
        service_name: Nombre del servicio
        level: Nivel de log (default: LOG_LEVEL env var o INFO)

    Returns:
        Logger con redaction automática.
    """
    from nickname_common.log_redactor import RedactingFilter

    logger = setup_logger(service_name, level)

    # Añadir RedactingFilter si no lo tiene ya
    has_redactor = any(isinstance(f, RedactingFilter) for f in logger.filters)
    if not has_redactor:
        logger.addFilter(RedactingFilter())

    return logger
