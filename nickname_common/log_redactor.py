"""
Log redaction — filtra secretos y datos sensibles antes de escribir a logs.

Uso:
    from nickname_common.logging import setup_logger
    from nickname_common.log_redactor import RedactingFilter

    logger = setup_logger("email-marketing")
    logger.addFilter(RedactingFilter())
    # A partir de aquí, cualquier log redacta automáticamente:
    logger.info(f"Calling Odoo with key={api_key}")
    # → [EMAIL-MARKETING] [INFO] Calling Odoo with key=***REDACTED***

O usar el helper integrado:
    from nickname_common import setup_logger_safe
    logger = setup_logger_safe("email-marketing")
    # Ya incluye RedactingFilter
"""

import logging
import re


# Patrones de secretos a redactar (ordenados de más específico a más genérico)
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Anthropic API keys
    (re.compile(r'sk-ant-[a-zA-Z0-9\-]{20,}'), '***ANTHROPIC_KEY***'),
    # OpenAI API keys
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), '***OPENAI_KEY***'),
    # HubSpot PAT
    (re.compile(r'pat-[a-zA-Z0-9]{4,}-[a-zA-Z0-9\-]{20,}'), '***HUBSPOT_PAT***'),
    # Google API keys
    (re.compile(r'AIza[0-9A-Za-z_\-]{35}'), '***GOOGLE_KEY***'),
    # Generic long hex tokens (40+ chars — likely API keys)
    (re.compile(r'(?<![a-fA-F0-9])[0-9a-fA-F]{40,}(?![a-fA-F0-9])'), '***HEX_TOKEN***'),
    # Bearer tokens
    (re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*', re.IGNORECASE), 'Bearer ***REDACTED***'),
    # Basic auth
    (re.compile(r'Basic\s+[A-Za-z0-9+/]+=*', re.IGNORECASE), 'Basic ***REDACTED***'),
    # Passwords / secrets in key=value pairs
    (re.compile(
        r'(password|passwd|pwd|secret|token|api_key|apikey|access_token)'
        r'\s*[=:]\s*["\']?([^\s"\'&]{8,})',
        re.IGNORECASE,
    ), r'\1=***REDACTED***'),
    # Email addresses → partial redaction (keep domain)
    (re.compile(r'[a-zA-Z0-9_.+-]+@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)'), r'***@\1'),
]


def redact(text: str) -> str:
    """Redacta secretos y datos sensibles de un string."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Filtro de logging que redacta secretos automáticamente.

    Se aplica a msg y args del LogRecord antes de formatear.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True
