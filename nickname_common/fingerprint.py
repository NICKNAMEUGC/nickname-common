"""
fingerprint() e idempotency_key() — huella estable y clave de idempotencia.

Nacen de SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911 (WI-1): los jobs
recurrentes en Railway necesitan (a) saber si "algo cambió" desde la última
ejecución sin invocar ningún LLM para decidirlo, y (b) una clave estable para
no procesar dos veces el mismo periodo aunque el job se relance (retry,
restart, doble trigger).

Uso típico — checker de delta (ver SPEC §7.2):
    huella_actual = fingerprint({"sha": sha, "ci": ci_status, "pr": pr_state})
    if huella_actual == huella_anterior:
        return  # changed=False, cero LLM — ver AutomationRunV1.changed

Uso típico — informe diario idempotente (ver SPEC §6):
    clave = idempotency_key("feedback", "2026-09-10")
    if ya_procesado(clave):
        return  # no relanzar el mismo periodo dos veces
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def fingerprint(obj: Any) -> str:
    """SHA-256 hex de la representación JSON canónica de `obj`.

    Canónica = claves ordenadas, sin espacios superfluos, ASCII. Dos llamadas
    con el mismo contenido — aunque el dict se haya construido en distinto
    orden de claves — producen la MISMA huella; cualquier diferencia real de
    contenido produce una huella distinta.

    `obj` debe ser JSON-serializable de forma determinista: dict/list/str/
    int/float/bool/None. NO pasar objetos con __dict__ arbitrario, sets
    (orden no determinista), datetimes sin convertir a string, ni nada cuyo
    `repr`/serialización pueda variar entre ejecuciones con el mismo
    contenido lógico — eso rompería silenciosamente la detección de cambios
    (una huella que varía sin que el contenido haya cambiado se interpreta
    como "hubo novedad" y dispara una llamada LLM innecesaria).
    """
    canonical = json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def idempotency_key(*parts: str) -> str:
    """Clave de idempotencia legible: partes unidas por ':'.

    NO es un hash — es la clave que un consumidor usa para detectar "ya
    procesé esto" (p.ej. "feedback:2026-09-10", "supervisor:WI-XYZ:<sha8>").
    Debe construirse SIEMPRE con datos deterministas del periodo/objeto que
    se procesa (fecha del corte, SHA observado), nunca con la hora de
    ejecución ni con un uuid — dos ejecuciones del mismo periodo deben
    producir la MISMA clave, o la idempotencia no protege nada.

    Cada parte se convierte a `str()` y no puede contener ':' (rompería el
    parseo inverso de quien lea la clave) ni estar vacía.
    """
    if not parts:
        raise ValueError("idempotency_key necesita al menos una parte")
    partes = [str(p) for p in parts]
    for p in partes:
        if not p:
            raise ValueError("idempotency_key no acepta partes vacías")
        if ":" in p:
            raise ValueError(
                f"idempotency_key: la parte {p!r} contiene ':' — "
                "rompería el parseo de la clave compuesta"
            )
    return ":".join(partes)
