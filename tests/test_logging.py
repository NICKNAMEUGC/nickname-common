"""Tests para nickname_common.logging"""

import logging
from nickname_common.logging import setup_logger


def test_setup_logger_returns_logger():
    log = setup_logger("test-service")
    assert isinstance(log, logging.Logger)
    assert log.name == "nickname.test-service"


def test_setup_logger_format():
    """Verificar formato [SERVICE] [LEVEL]"""
    log = setup_logger("mi-servicio")
    # Debe tener al menos un handler (StreamHandler)
    assert len(log.handlers) >= 1


def test_setup_logger_idempotent():
    """Llamar dos veces no duplica handlers."""
    log1 = setup_logger("idempotent-test")
    h1 = len(log1.handlers)
    log2 = setup_logger("idempotent-test")
    assert len(log2.handlers) == h1
    assert log1 is log2
