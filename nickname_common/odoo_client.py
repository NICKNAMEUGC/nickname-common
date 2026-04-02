"""
OdooService base — Cliente XML-RPC para Odoo 18 con circuit breaker y thread-safety.

Extraído de nickname-email-marketing-agent y nickname-odoo-agent.

Uso:
    from nickname_common.odoo_client import OdooService
    odoo = OdooService()  # Lee config de env vars
    partners = odoo.search_read('res.partner', [['is_company', '=', True]], fields=['name'])

MANDATORY: company_id=2 (NICKNAME MANAGEMENT SL.) por defecto.
"""

import os
import threading
import time
import xmlrpc.client
from typing import Any, Dict, List, Optional

from nickname_common.logging import setup_logger

log = setup_logger("odoo-client")

# --- Configuración ---
XMLRPC_TIMEOUT = int(os.getenv("ODOO_XMLRPC_TIMEOUT", "20"))
CIRCUIT_BREAKER_TIMEOUT = int(os.getenv("ODOO_CIRCUIT_BREAKER_TIMEOUT", "60"))


# --- Transports con timeout ---

class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """Transport HTTPS con timeout configurable."""

    def __init__(self, timeout=XMLRPC_TIMEOUT, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutTransport(xmlrpc.client.Transport):
    """Transport HTTP con timeout configurable."""

    def __init__(self, timeout=XMLRPC_TIMEOUT, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _make_proxy(url: str) -> xmlrpc.client.ServerProxy:
    """Crea un ServerProxy con timeout."""
    if url.startswith("https"):
        transport = _TimeoutSafeTransport(timeout=XMLRPC_TIMEOUT)
    else:
        transport = _TimeoutTransport(timeout=XMLRPC_TIMEOUT)
    return xmlrpc.client.ServerProxy(url, transport=transport)


class OdooService:
    """Cliente XML-RPC para Odoo 18 con circuit breaker y thread-safety.

    Features:
    - Lazy init de conexión (authenticate al primer uso)
    - Circuit breaker: evita bloqueos de 20s cuando Odoo es inaccesible
    - Thread-safe: RLock serializa llamadas concurrentes (APScheduler safe)
    - Timeout configurable en conexiones XML-RPC
    """

    def __init__(self, url: str = None, db: str = None, username: str = None,
                 api_key: str = None, company_id: int = None):
        self.url = url or os.getenv("ODOO_URL")
        self.db = db or os.getenv("ODOO_DB")
        self.username = username or os.getenv("ODOO_USERNAME")
        self.api_key = api_key or os.getenv("ODOO_API_KEY")
        self.company_id = company_id or int(os.getenv("ODOO_COMPANY_ID", "2"))

        self._uid = None
        self._common = None
        self._models = None

        # Thread-safety: RLock porque execute() → self.models/uid (lazy init recursivo)
        self._lock = threading.RLock()

        # Circuit breaker
        self._circuit_open_until = 0.0
        self._last_error = None

    @property
    def uid(self) -> int:
        if self._uid is None:
            with self._lock:
                if self._uid is None:
                    self._common = _make_proxy(f"{self.url}/xmlrpc/2/common")
                    self._uid = self._common.authenticate(
                        self.db, self.username, self.api_key, {}
                    )
                    if not self._uid:
                        raise ConnectionError(
                            f"No se pudo autenticar con Odoo como {self.username}"
                        )
        return self._uid

    @property
    def models(self):
        if self._models is None:
            with self._lock:
                if self._models is None:
                    self._models = _make_proxy(f"{self.url}/xmlrpc/2/object")
        return self._models

    @property
    def is_available(self) -> bool:
        """True si el circuit breaker está cerrado."""
        return time.time() >= self._circuit_open_until

    def _open_circuit(self, error: Exception):
        """Abre el circuit breaker tras un fallo de conexión."""
        self._circuit_open_until = time.time() + CIRCUIT_BREAKER_TIMEOUT
        self._last_error = str(error)
        log.warning(
            f"[CIRCUIT-BREAKER] Odoo inaccesible — circuito abierto "
            f"{CIRCUIT_BREAKER_TIMEOUT}s: {error}"
        )

    def _close_circuit(self):
        """Cierra el circuit breaker tras una llamada exitosa."""
        if self._circuit_open_until > 0:
            log.info("[CIRCUIT-BREAKER] Odoo accesible — circuito cerrado")
            self._circuit_open_until = 0.0
            self._last_error = None

    def execute(self, model: str, method: str, *args, **kwargs) -> Any:
        """Ejecuta una llamada XML-RPC a Odoo con circuit breaker y lock."""
        if not self.is_available:
            raise ConnectionError(
                f"Odoo no disponible (circuit breaker abierto, retry en "
                f"{int(self._circuit_open_until - time.time())}s): {self._last_error}"
            )
        try:
            with self._lock:
                result = self.models.execute_kw(
                    self.db, self.uid, self.api_key,
                    model, method, list(args), kwargs
                )
            self._close_circuit()
            return result
        except xmlrpc.client.Fault as e:
            # Odoo está activo pero rechazó la llamada — NO abrir circuit breaker.
            # Si el faultCode indica error de autenticación, invalidar UID cacheado.
            auth_fault_codes = {1, 3}
            is_auth_fault = (
                e.faultCode in auth_fault_codes
                or "access denied" in str(e.faultString).lower()
            )
            if is_auth_fault:
                log.warning(
                    f"[ODOO] Fault de autenticación (faultCode={e.faultCode}) — "
                    f"invalidando UID cacheado para forzar re-auth: {e.faultString}"
                )
                with self._lock:
                    self._uid = None
            else:
                log.error(
                    f"[ODOO] xmlrpc.Fault en {model}.{method} "
                    f"(faultCode={e.faultCode}): {e.faultString}"
                )
            raise
        except (ConnectionError, TimeoutError, OSError, xmlrpc.client.ProtocolError) as e:
            self._open_circuit(e)
            raise

    def execute_with_context(self, model: str, method: str, args: list,
                             context: Optional[Dict] = None) -> Any:
        """Ejecuta con contexto explícito (para campos property dependientes de compañía)."""
        if not self.is_available:
            raise ConnectionError(
                f"Odoo no disponible (circuit breaker abierto): {self._last_error}"
            )
        ctx = context or {
            "force_company": self.company_id,
            "allowed_company_ids": [self.company_id],
        }
        try:
            with self._lock:
                result = self.models.execute_kw(
                    self.db, self.uid, self.api_key,
                    model, method, args, {"context": ctx}
                )
            self._close_circuit()
            return result
        except xmlrpc.client.Fault as e:
            auth_fault_codes = {1, 3}
            is_auth_fault = (
                e.faultCode in auth_fault_codes
                or "access denied" in str(e.faultString).lower()
            )
            if is_auth_fault:
                log.warning(
                    f"[ODOO] Fault de autenticación (faultCode={e.faultCode}) — "
                    f"invalidando UID cacheado para forzar re-auth: {e.faultString}"
                )
                with self._lock:
                    self._uid = None
            else:
                log.error(
                    f"[ODOO] xmlrpc.Fault en execute_with_context "
                    f"(faultCode={e.faultCode}): {e.faultString}"
                )
            raise
        except (ConnectionError, TimeoutError, OSError, xmlrpc.client.ProtocolError) as e:
            self._open_circuit(e)
            raise

    # --- CRUD helpers ---

    def search(self, model: str, domain: list, **kwargs) -> List[int]:
        return self.execute(model, "search", domain, **kwargs)

    def search_read(self, model: str, domain: list, fields: List[str] = None,
                    **kwargs) -> List[Dict]:
        kwargs_full = {}
        if fields:
            kwargs_full["fields"] = fields
        kwargs_full.update(kwargs)
        return self.execute(model, "search_read", domain, **kwargs_full)

    def read(self, model: str, ids: List[int], fields: List[str] = None) -> List[Dict]:
        args = [ids]
        if fields:
            args.append(fields)
        return self.execute(model, "read", *args)

    def create(self, model: str, vals: Dict) -> int:
        return self.execute(model, "create", vals)

    def write(self, model: str, ids: List[int], vals: Dict) -> bool:
        return self.execute(model, "write", ids, vals)

    def unlink(self, model: str, ids: List[int]) -> bool:
        return self.execute(model, "unlink", ids)

    def count(self, model: str, domain: list) -> int:
        return self.execute(model, "search_count", domain)

    def write_with_company_context(self, model: str, ids: List[int], vals: Dict) -> bool:
        """Write con contexto de compañía forzado (para campos property)."""
        return self.execute_with_context(
            model, "write", [ids, vals],
            context={
                "force_company": self.company_id,
                "allowed_company_ids": [self.company_id],
            },
        )

    def test_connection(self) -> Dict:
        """Test de conexión con Odoo. Devuelve status dict."""
        if not self.is_available:
            return {
                "status": "circuit_open",
                "error": self._last_error,
                "retry_in_seconds": max(0, int(self._circuit_open_until - time.time())),
                "url": self.url,
            }
        try:
            uid = self.uid
            version = self._common.version()
            self._close_circuit()
            return {
                "status": "connected",
                "uid": uid,
                "server_version": version.get("server_version", "unknown"),
                "url": self.url,
            }
        except (ConnectionError, TimeoutError, OSError, xmlrpc.client.ProtocolError) as e:
            self._open_circuit(e)
            return {"status": "error", "error": str(e), "url": self.url}
        except Exception as e:
            return {"status": "error", "error": str(e), "url": self.url}
