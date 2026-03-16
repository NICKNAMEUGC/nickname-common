"""
nickname-common — Paquete compartido para los agentes de Nickname Management.

Módulos:
  - health: DeepHealthChecker reutilizable con ejecución paralela
  - logging: setup_logger(service_name) con formato estándar
  - odoo_client: OdooService base (XML-RPC + circuit breaker + thread-safety)
  - hubspot_client: HubSpotService base (REST + rate limiting)
  - config: load_config() con validación de variables de entorno
"""

__version__ = "0.1.0"

from nickname_common.health import DeepHealthChecker
from nickname_common.logging import setup_logger
from nickname_common.config import load_config
