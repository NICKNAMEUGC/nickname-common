"""Tests para nickname_common.log_redactor — redacción de secretos en logs."""

import logging

from nickname_common.log_redactor import redact, RedactingFilter
from nickname_common.logging import setup_logger_safe


# --- redact() function ---

def test_redact_anthropic_key():
    result = redact("key sk-ant-abc123def456ghi789jkl012mno345pqr678stu")
    assert "***ANTHROPIC_KEY***" in result
    assert "sk-ant" not in result


def test_redact_openai_key():
    result = redact("key=sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD")
    assert "***OPENAI_KEY***" in result


def test_redact_hubspot_pat():
    result = redact("pat-na1a-abc123def456ghi789jklmnopqrstuvwxyz01234")
    assert "***HUBSPOT_PAT***" in result


def test_redact_google_key():
    result = redact("AIzaSyD1234567890abcdefghijklmnopqrstuv")
    assert "***GOOGLE_KEY***" in result


def test_redact_hex_token():
    result = redact("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    assert "***HEX_TOKEN***" in result


def test_redact_bearer_token():
    result = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc")
    assert "Bearer ***REDACTED***" in result


def test_redact_basic_auth():
    result = redact("Basic dXNlcm5hbWU6cGFzc3dvcmQ=")
    assert "Basic ***REDACTED***" in result


def test_redact_password():
    result = redact("password=my_super_secret_pwd_123")
    assert "***REDACTED***" in result
    assert "my_super_secret" not in result


def test_redact_email_partial():
    result = redact("user: diego@nickname.com")
    assert "***@nickname.com" in result
    assert "diego@" not in result


def test_redact_safe_text_unchanged():
    safe = "Worker iniciado en puerto 5056"
    assert redact(safe) == safe


def test_redact_multiple_secrets():
    text = "key=sk-ant-aaabbbcccdddeeefffggghhhiiijjjkkklllmmm email=diego@test.com"
    result = redact(text)
    assert "***ANTHROPIC_KEY***" in result
    assert "***@test.com" in result


# --- RedactingFilter ---

def test_redacting_filter_on_msg():
    filt = RedactingFilter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="got key sk-ant-aaabbbcccdddeeefffggghhhiiijjjkkklllmmm from env",
        args=None, exc_info=None,
    )
    filt.filter(record)
    assert "***ANTHROPIC_KEY***" in record.msg
    assert "sk-ant" not in record.msg


def test_redacting_filter_on_args_tuple():
    filt = RedactingFilter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="Got %s", args=("sk-ant-aaabbbcccdddeeefffggghhhiiijjjkkklllmmm",),
        exc_info=None,
    )
    filt.filter(record)
    assert "***ANTHROPIC_KEY***" in record.args[0]


# --- setup_logger_safe ---

def test_setup_logger_safe_has_filter():
    logger = setup_logger_safe("test-safe-logger")
    has_redactor = any(isinstance(f, RedactingFilter) for f in logger.filters)
    assert has_redactor


def test_setup_logger_safe_idempotent():
    """Llamar dos veces no duplica el filtro."""
    logger1 = setup_logger_safe("test-idempotent")
    logger2 = setup_logger_safe("test-idempotent")
    assert logger1 is logger2
    redactor_count = sum(1 for f in logger1.filters if isinstance(f, RedactingFilter))
    assert redactor_count == 1
