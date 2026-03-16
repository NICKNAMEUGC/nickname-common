"""
Config loader — Validación de variables de entorno con defaults opcionales.

Uso:
    from nickname_common import load_config

    config = load_config(
        required=["ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_API_KEY"],
        optional={"ODOO_COMPANY_ID": "2", "LOG_LEVEL": "INFO"},
    )
    # config["ODOO_URL"] → valor del env var
    # config["ODOO_COMPANY_ID"] → valor del env var o "2" por defecto
    # Lanza ValueError si falta alguna required
"""

import os
from typing import Dict, List, Optional


def load_config(required: List[str] = None,
                optional: Dict[str, str] = None) -> Dict[str, str]:
    """Carga y valida variables de entorno.

    Args:
        required: Lista de variables obligatorias. Lanza ValueError si alguna falta.
        optional: Dict de variable → default. Usa el default si la variable no existe.

    Returns:
        Dict con todas las variables (required + optional) y sus valores.

    Raises:
        ValueError: Si alguna variable required no está definida o está vacía.
    """
    config = {}
    missing = []

    # Variables obligatorias
    for var in (required or []):
        value = os.getenv(var, "").strip()
        if not value:
            missing.append(var)
        else:
            config[var] = value

    if missing:
        raise ValueError(
            f"Variables de entorno obligatorias no configuradas: {', '.join(missing)}"
        )

    # Variables opcionales con defaults
    for var, default in (optional or {}).items():
        value = os.getenv(var, "").strip()
        config[var] = value if value else default

    return config
