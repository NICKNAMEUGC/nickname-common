"""
DeepHealthChecker — Health check reutilizable con ejecución paralela y timeout.

Uso:
    checker = DeepHealthChecker(service_name="email-marketing", version="1.2.0")
    checker.add_check("odoo", lambda: odoo.search_read('res.company', [['id','=',2]], ['name']))
    checker.add_check("hubspot", lambda: hs.get('/crm/v3/objects/deals?limit=1'))
    result = checker.run()  # dict listo para jsonify
"""

import time
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


DEFAULT_TIMEOUT_SEC = 5


class DeepHealthChecker:
    """Ejecuta checks de salud en paralelo con timeout individual."""

    def __init__(self, service_name: str, version: str = "0.0.0",
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC):
        self.service_name = service_name
        self.version = version
        self.timeout_sec = timeout_sec
        self._checks: List[Dict] = []

    def add_check(self, name: str, fn: Callable[[], Any],
                  timeout_sec: Optional[float] = None):
        """Registra un check de salud.

        Args:
            name: Nombre del check (ej: 'odoo', 'hubspot', 'google_sheets')
            fn: Callable sin argumentos. Si no lanza excepción, se considera OK.
                Puede devolver un string con detalle (opcional).
            timeout_sec: Timeout individual para este check (hereda del checker si None)
        """
        self._checks.append({
            "name": name,
            "fn": fn,
            "timeout_sec": timeout_sec or self.timeout_sec,
        })

    def run(self) -> Dict:
        """Ejecuta todos los checks en paralelo y devuelve resultado estandarizado.

        Formato de respuesta:
        {
            "service": "email-marketing",
            "version": "1.2.0",
            "status": "online" | "degraded" | "offline",
            "timestamp": "2026-03-16T10:00:00+00:00",
            "checks": {
                "odoo": {"status": "ok", "latency_ms": 142, "detail": "..."},
                "hubspot": {"status": "error", "latency_ms": 5001, "detail": "timeout"}
            }
        }
        """
        results: Dict[str, Dict] = {}
        threads: List[threading.Thread] = []

        def _run_check(check: Dict):
            name = check["name"]
            fn = check["fn"]
            timeout = check["timeout_sec"]
            t0 = time.time()

            # Ejecutar en sub-thread con timeout
            result_holder = {"value": None, "error": None}
            done_event = threading.Event()

            def _exec():
                try:
                    result_holder["value"] = fn()
                except Exception as e:
                    result_holder["error"] = e
                finally:
                    done_event.set()

            worker = threading.Thread(target=_exec, daemon=True)
            worker.start()
            done_event.wait(timeout=timeout)

            latency = round((time.time() - t0) * 1000)

            if not done_event.is_set():
                results[name] = {
                    "status": "error",
                    "latency_ms": latency,
                    "detail": f"Timeout ({timeout}s)",
                }
            elif result_holder["error"]:
                results[name] = {
                    "status": "error",
                    "latency_ms": latency,
                    "detail": str(result_holder["error"])[:200],
                }
            else:
                detail = str(result_holder["value"])[:200] if result_holder["value"] else "ok"
                results[name] = {
                    "status": "ok",
                    "latency_ms": latency,
                    "detail": detail,
                }

        # Lanzar checks en paralelo
        for check in self._checks:
            t = threading.Thread(target=_run_check, args=(check,), daemon=True)
            threads.append(t)
            t.start()

        # Esperar a que todos terminen (con timeout global generoso)
        max_timeout = max((c["timeout_sec"] for c in self._checks), default=self.timeout_sec)
        for t in threads:
            t.join(timeout=max_timeout + 1)

        # Calcular estado global
        statuses = [c["status"] for c in results.values()]
        if not statuses:
            overall = "online"
        elif all(s == "ok" for s in statuses):
            overall = "online"
        elif all(s == "error" for s in statuses):
            overall = "offline"
        else:
            overall = "degraded"

        return {
            "service": self.service_name,
            "version": self.version,
            "status": overall,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": results,
        }
