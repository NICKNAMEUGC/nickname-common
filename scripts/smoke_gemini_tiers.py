#!/usr/bin/env python3
"""Smoke REAL de los tiers Gemini (D-F, 2026-10-18). NO se ejecuta en CI.

Una llamada mínima por tier y por vía, a través de `complete_for(...,
tier=...)` (así se prueba también el transporte nuevo: tier fijado,
data_collection="deny", reasoning por defecto, finish_reason y el evento):

    gemini_flash_lite / gemini_flash / gemini_pro  ×  gemini_direct / openrouter

- Vía directa: `NK_ROUTER=off` en el proceso (los pasos OpenRouter se saltan).
- Vía OpenRouter: sin `GEMINI_API_KEY` en el proceso (el paso directo se salta).
- `gemini_flash_lite` va con json_schema; el resto, texto libre.
- `max_tokens` pequeño (64 en flash; 512 en pro, que solo funciona razonando,
  con `reasoning={"effort": "low"}`).

Claves: `GEMINI_API_KEY` y `OPENROUTER_API_KEY` del ENTORNO; el script nunca
las lee de ficheros ni las imprime. Exige `--live` para gastar (céntimos).
Salida: un resumen JSON sin contenido (ni prompt ni respuesta): tier, vía,
proveedor, ok/fallo, finish_reason, is_byok, tokens, coste y, en OpenRouter,
el coste upstream frente al del catálogo (contraste de precios).

- En OpenRouter consulta además GET /api/v1/generation?id=<id> y anota
  `provider_name` e `is_byok` (comprueba el `provider.order` hacia
  google-ai-studio, donde está la clave BYOK).

Uso:
    GEMINI_API_KEY=... OPENROUTER_API_KEY=... \\
        python scripts/smoke_gemini_tiers.py --live [--tiers gemini_pro] [--out docs/evidence/x.json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

# Ejecutable desde un checkout sin instalar el paquete.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from nickname_common import llm
from nickname_common.cost_catalog import CATALOG_VERSION, estimate_cost_micros

TIERS = ("gemini_flash_lite", "gemini_flash", "gemini_pro")
CLASE = "clasificacion_masiva"  # sus vías: gemini_direct → openrouter
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
PROMPT = [{"role": "user", "content": 'Responde exactamente {"ok": true} si me lees.'}]
PROMPT_TEXTO = [{"role": "user", "content": "Responde solo con la palabra: ok"}]


def _generation(gen_id: str) -> dict:
    """provider_name / is_byok de una generación de OpenRouter (sin contenido)."""
    headers = {"Authorization": f"Bearer {os.environ[llm.ROUTER_KEY_ENV].strip()}"}
    for _ in range(8):
        time.sleep(2)
        req = urllib.request.Request(
            f"https://openrouter.ai/api/v1/generation?id={gen_id}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())["data"]
        except urllib.error.HTTPError:
            continue
        return {k: data.get(k) for k in (
            "provider_name", "is_byok", "upstream_inference_cost",
            "native_tokens_prompt", "native_tokens_completion", "native_tokens_reasoning")}
    return {"error": "generation_no_disponible"}


def _una(tier: str, via: str) -> dict:
    saved = {k: os.environ.get(k) for k in (llm.ROUTER_ENV, llm.GEMINI_KEY_ENV, llm.GEMINI_KEY_FALLBACK_ENV)}
    try:
        if via == "gemini_direct":
            os.environ[llm.ROUTER_ENV] = "off"
        else:
            os.environ.pop(llm.ROUTER_ENV, None)
            os.environ.pop(llm.GEMINI_KEY_ENV, None)
            os.environ.pop(llm.GEMINI_KEY_FALLBACK_ENV, None)
        pro = tier == "gemini_pro"
        con_schema = tier == "gemini_flash_lite"
        fila = {"tier": tier, "via": via, "model": llm.get_model(tier) if via == "gemini_direct"
                else llm.router_slug(tier), "json_schema": con_schema}
        ids: list[str] = []

        def post(url, headers, body, timeout):
            status, raw = llm._urllib_post(url, headers, body, timeout)  # noqa: SLF001
            if "openrouter.ai" in url and status == 200:
                ids.append(json.loads(raw).get("id", ""))
            return status, raw

        try:
            res = llm.complete_for(
                CLASE,
                PROMPT if con_schema else PROMPT_TEXTO,
                guard=None,
                budget_exempt="smoke-D-F-20261018",
                tier=tier,
                json_schema=SCHEMA if con_schema else None,
                max_tokens=512 if pro else 64,
                reasoning={"effort": "low"} if pro else None,
                timeout=90,
                job_id="smoke_gemini_tiers",
                http_post=post,
            )
        except Exception as exc:  # noqa: BLE001 — el smoke resume, no relanza
            fila.update(ok=False, error=type(exc).__name__,
                        detalle=str(exc)[:300] if isinstance(exc, llm.CascadeExhausted) else None)
            return fila
        u = res.usage
        fila.update(
            ok=bool(res.text.strip()) and (not con_schema or res.parsed_json is not None),
            provider=res.provider,
            finish_reason=res.finish_reason,
            outcome=res.usage_event.outcome.value,
            is_byok=bool(u["is_byok"]) if "is_byok" in u else None,
            input_tokens=u.get("prompt_tokens"),
            output_tokens=res.usage_event.output_tokens,
            cost_micros=res.usage_event.cost_micros,
            cost_source=res.usage_event.cost_source.value,
        )
        if via == "openrouter":
            catalogo = estimate_cost_micros(
                llm.get_model(tier), input_tokens=u.get("prompt_tokens"),
                output_tokens=res.usage_event.output_tokens,
            )
            fila.update(upstream_cost_micros=u.get("upstream_cost_micros"),
                        openrouter_fee_micros=u.get("cost_micros"),
                        catalog_estimate_micros=catalogo,
                        generation=_generation(ids[-1]) if ids and ids[-1] else None)
        return fila
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--live", action="store_true", help="llamar de verdad (gasta céntimos)")
    ap.add_argument("--out", help="ruta donde guardar el resumen JSON (sin contenido)")
    ap.add_argument("--tiers", nargs="+", choices=TIERS, default=list(TIERS),
                    help="tiers a probar (por defecto, los tres)")
    args = ap.parse_args(argv)
    if not args.live:
        print("Sin --live no se llama a nada.", file=sys.stderr)
        return 2
    faltan = [k for k in (llm.GEMINI_KEY_ENV, llm.ROUTER_KEY_ENV) if not os.getenv(k, "").strip()]
    if faltan:
        print(f"Faltan en el entorno: {', '.join(faltan)}", file=sys.stderr)
        return 2
    os.environ.setdefault(llm.ROUTER_AGENT_ENV, "nickname-common-smoke")
    filas = [_una(tier, via) for tier in args.tiers for via in ("gemini_direct", "openrouter")]
    resumen = {
        "smoke": "gemini_tiers_D-F",
        "fecha_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "catalog_version": CATALOG_VERSION,
        "resultados": filas,
        "todo_ok": all(f["ok"] for f in filas),
    }
    texto = json.dumps(resumen, ensure_ascii=False, indent=2)
    print(texto)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(texto + "\n")
    return 0 if resumen["todo_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
