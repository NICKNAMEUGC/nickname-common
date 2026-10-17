<!-- DOC-TYPE: AUTORITATIVO -->
# nickname-common

Librería Python compartida por los agentes del ecosistema (~14 repos la consumen).
Solo stdlib, cero dependencias externas. Aporta: logging estándar, validación de
config, health checks, clientes Odoo/HubSpot, registro central de modelos LLM y
política del plano IA por clase de trabajo (`ai_policy.py`),
presupuesto/observabilidad de gasto LLM (`budget.py`, `cost_catalog.py`,
`models/llm_usage.py`), huellas de idempotencia (`fingerprint.py`), eval
harness, activity logging y modelos de datos.

GitHub: https://github.com/NICKNAMEUGC/nickname-common · v0.2.0 · Python >=3.12 · CI: `.github/workflows/verify.yml` (pytest en PR)

## Reglas del repo
- Cero dependencias externas (`setup.py` con `install_requires=[]`). Cada agente trae las suyas (Flask, etc.).
- NO tiene `.env` propio — hereda del agente que lo importa (`.env.example` documenta las vars).
- Cambios aquí afectan a TODOS los consumidores: tests + `verify.sh` en verde antes de push, y bump del pin en los agentes que deban recibir el cambio (sin bump, Railway no lo ve).
- Contexto Odoo del tenant (companies, stages, gotchas): `.ag/odoo_reference.md`. Servicios y reglas técnicas: `.ag/api/registry.yaml`.

## Instalación
```bash
# Desarrollo local (editable)
pip install -e ~/Desktop/Apps/nickname-common

# En requirements.txt de agentes: pin por SHA (obtenerlo del repo, no de este doc)
git -C ~/Desktop/Apps/nickname-common rev-parse HEAD
# nickname-common @ git+https://github.com/NICKNAMEUGC/nickname-common.git@<SHA>
```

## API pública
```python
from nickname_common import (
    setup_logger,        # [SERVICE] [LEVEL] msg — respeta LOG_LEVEL, cachea por nombre
    setup_logger_safe,   # igual + RedactingFilter (redacción NO es default)
    load_config,         # load_config(required=[...], optional={...}) — ValueError si falta required
    DeepHealthChecker,   # checks paralelos con timeout → status online/degraded/offline
    RedactingFilter, redact,  # enmascara keys (Anthropic/OpenAI/Google/HubSpot), Bearer/Basic, hex≥40, passwords, emails
)
from nickname_common.odoo_client import OdooService
#   XML-RPC + circuit breaker (60s tras fallo de red/timeout — NO de auth) + lazy auth + RLock. company_id default=2 (hardcoded).
#   search / search_read / read / create / write / unlink / execute_with_context / test_connection
from nickname_common.hubspot_client import HubSpotService
#   REST + retry en 429 (10s x intento, max 3). search_all (paginado) / search_modified / get_associations
from nickname_common.llm import get_model, all_models, all_tiers, provider_of, capabilities, is_routable, gemini_config_sdk, gemini_config_rest
from nickname_common.llm import complete_for, ClassNotApiExecutable, CascadeExhausted  # puerta NUEVA (plano IA, ola 1)
from nickname_common.llm import complete    # LEGADO: se conserva para quien ya la usa; código nuevo → complete_for
from nickname_common.ai_policy import policy_for, api_chain, subscription_chain, all_classes, as_dict, POLICY_VERSION
from nickname_common.evals import run_golden, load_golden   # EvalReport con accuracy
from nickname_common.activity_logger import ActivityLogger  # escribe .ag/decisions_log.md (fcntl + Lock)
from nickname_common.models import (
    Task, TaskStatus, TaskPriority, TasksResponse,
    HealthCheck, HealthResponse, ServiceStatus,
    AutomationJob, AutomationSeverity, AutomationsResponse,
    ActivityEntry, ActivityLevel,
    AutomationRunV1, LLMUsageEventV1, RunStatus, SourceStatus, LLMOutcome, CostSource,
)
from nickname_common.budget import BudgetGuard, BudgetStore, BudgetStatus, BudgetExhausted, BudgetBlockedReason
from nickname_common.cost_catalog import estimate_cost_micros, known_models, CATALOG_VERSION
from nickname_common.fingerprint import fingerprint, idempotency_key
```

## Registro LLM (`llm.py`) — fuente única de nombres de modelo
- Tiers: `gemini_flash`, `gemini_flash_lite`, `gemini_pro`, `imagen`, `gemini_embedding`, `claude_sonnet`, `claude_opus`, `claude_haiku`, `gpt_quality`, `gpt_fast`, `grok_fast`, `grok_quality` → `get_model(tier)`. Los ids concretos viven SOLO en `_DEFAULTS` / `_ROUTER_SLUGS` de `llm.py` (no copiarlos aquí); `provider_of()` devuelve `gemini` | `anthropic` | `openai` | `xai`.
- **Reparación 2026-10-17 (ola 1 del plano IA)**: `claude_sonnet` y `grok_fast`/`grok_quality` apuntaban a modelos que ya no existen (verificado por API; histórico de retiradas en el comentario de `_DEFAULTS`). Se sustituyeron por los vigentes de su familia: es reparación, no cambio de calidad. El refresco de calidad del resto (Gemini 3.x) es la decisión D-E, servicio a servicio con su eval (ola 2). Ojo al subir el pin: la familia Claude 5.x rechaza `temperature` (Opus siempre; Sonnet/Haiku con valores no default) — un consumidor que llame a Anthropic con `get_model("claude_sonnet")` y `temperature` recibirá 400.
- Tiers nuevos (ola 1): `claude_opus`, `claude_haiku`, `gpt_quality`, `gpt_fast` (chat, enrutables) y `gemini_embedding` (NO enrutable ni de chat, como `imagen`: se llama por su endpoint con `get_model`). Guard L-014 por familia: `claude-*`, `gpt-*`, `gemini-embedding-*` (no `text-embedding-*`, que es también OpenAI). Los ids directos de xAI no son verificables sin `XAI_API_KEY`: los `grok_*` siguen dry.
- `gemini_flash_lite` (2026-09-11, SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911 WI-1): tier de coste mínimo para los jobs recurrentes en Railway — clasificar un delta ya acotado, resumir comentarios anonimizados. Misma familia/capacidades que `gemini_flash` (`capabilities("gemini_flash_lite") == capabilities("gemini_flash")`), mismo `thinking_budget=0` por defecto, mismo trato en la cascada (`google-direct` incluido en `_GEMINI_DIRECT_TIERS`). Slug OpenRouter: `google/gemini-2.5-flash-lite`.
- Override de emergencia sin deploy: env `NK_MODEL_<TIER>` (p.ej. `NK_MODEL_GEMINI_FLASH`) en Railway.
- Guard L-014: un override no puede cruzar de familia (`NK_MODEL_GEMINI_FLASH=grok-4` → `CrossProviderOverrideError`).
- `gemini_config_sdk()` / `gemini_config_rest()` ponen `thinking_budget=0` por defecto (protege `max_output_tokens`).
- ⚠️ `gemini-2.5-pro` RECHAZA `thinking_budget=0` (400) → para el tier `gemini_pro` pasar `thinking_budget=None`.
- xAI Fase 1 (dry): `complete(tier, messages, ...)` solo para `grok_*`. Sin `XAI_API_KEY` o sin `NK_XAI_LIVE=1` falla cerrado y no llama a la red. Tests inyectan `http_post`. Grok no cubre embeddings ni imagen (`capabilities()`).
- **Cascada de fallback** (mandato 2026-08-31, `.ag/decisions_log.md` → LLM-FALLBACK-OPENROUTER-MANDATORY): con `NK_ROUTER=openrouter`, un fallo de TRANSPORTE (HTTP ≥400, red, timeout, o falta de `OPENROUTER_API_KEY`) cae al siguiente proveedor con credencial: `openrouter → gemini_directo (solo tiers gemini) → openai → anthropic`. Un 200 con JSON inválido NO encadena (`parsed_json=None` como siempre). `LLMResult.provider` etiqueta el leg real: `openrouter:google` / `google-direct` / `openai-fallback` / `anthropic-fallback`. Si todo falla: `CascadeExhausted` (hereda de `ProviderNotConfigured`) con la cadena de intentos sin contenido. Los legs openai/anthropic cruzan familia POR DISEÑO (el guard L-014 es de overrides de env, no de la cascada). Embeddings/imagen siguen sin enrutarse ni fallbackearse; el leg anthropic no tiene `response_format` nativo — schema por instrucción de sistema + parseo.
- **Plano IA — `ai_policy.py` es la FUENTE CANÓNICA de la política por clase de trabajo** (SPEC-AI-PLANE-20261017 §4; decisiones D1, D2, D3, D4 y D-A de Diego del 2026-10-17). Vive en este paquete y no en un YAML de `apps-config` porque los servicios de Railway no tienen ese repo; cualquier export (broker, Command Center) se genera con `ai_policy.as_dict()`. Clases: `clasificacion_masiva`, `cliente_dinero_tiempo_real`, `cliente_dinero_asincrono`, `codigo`, `revision`, `contexto_largo`, `coordinacion`, `embeddings`, `imagen`. Cada una declara `lane` (`api` | `subscription`), `api_chain` (pasos `{tier, via}`, vía ∈ `openrouter` / `anthropic_direct` / `openai_direct` / `gemini_direct`) y `subscription_chain` (CLIs `claude` / `codex` / `gemini` / `grok` para el broker de la ola 4; la regla «familia distinta a la del autor» de `revision` la aplica el broker). Se valida AL IMPORTAR: tier inexistente, vía incoherente con la familia o embeddings/imagen por OpenRouter rompen el arranque, no la primera llamada. La política habla de tiers, nunca de modelos.
- **D-A (híbrido)**: Claude sale por OpenRouter con BYOK hacia la organización de Anthropic (lo absorbe el crédito de API del plan Team; cifra y cuenta en la SPEC §0) y cada agente conserva su clave OpenRouter con tope diario. Además, `claude_sonnet@anthropic_direct` es el respaldo si OpenRouter cae, pero solo se ejecuta donde existe `ANTHROPIC_API_KEY`: por decisión de Diego, únicamente `nickname-recobro-agent`, `nickname-facturas-agent` y los jobs CFO de `nickname-odoo-agent`. El BYOK se configura en la cuenta de OpenRouter, no en código.
- **`complete_for(clase, messages, *, guard, budget_exempt=None, json_schema=None, max_tokens=1024, temperature=None, http_post=None, timeout=30, service/job_id/run_id/reason)`** — la puerta nueva. Recorre la `api_chain` de la clase reutilizando los legs de la cascada:
  - Paso `@openrouter` solo con `NK_ROUTER=openrouter` **y** `OPENROUTER_API_KEY` (quitar `NK_ROUTER` sigue siendo el rollback sin deploy). Paso directo solo con su clave. Un paso sin credencial se salta y queda en la lista de intentos (`router-apagado` / `sin-credencial`).
  - Solo los fallos de TRANSPORTE encadenan (HTTP ≥400, red, timeout); un 200 con JSON inválido no. Todo agotado → `CascadeExhausted` con `vía[tier](motivo)` y `exc.usage_event`, sin contenido. Clase sin `api_chain` (carril suscripción) o de tiers que no son de chat → `ClassNotApiExecutable` (ValueError), sin red.
  - **Presupuesto obligatorio**: `guard` es un argumento requerido; `guard=None` exige `budget_exempt="<motivo>"` (si no, ValueError). Con guard: `check_before_call()` antes del primer paso (el `BudgetExhausted` sale sin tocar la red y con `exc.usage_event` budget_blocked); tras el éxito `record_call(coste)` y `check_call_cost(coste)` — si se supera el tope por llamada se propaga `BudgetExhausted` con la respuesta pagada en `exc.result`.
  - Coste: el del proveedor (con BYOK se toma el mayor entre `usage.cost` y `cost_details.upstream_inference_cost`, para no infravalorar ni contar dos veces) → catálogo verificado → `None`/`unavailable`, nunca 0. El resultado lleva `usage_event` (`LLMUsageEventV1`, sin PII ni prompt; `service` sale de `NK_AGENT_NAME`).
  - `LLMResult.provider` etiqueta el paso real: `openrouter:anthropic` / `openrouter:openai` / `openrouter:google` / `anthropic-direct` / `openai-direct` / `google-direct`. Diferencias con los legs legados, solo en `complete_for`: `temperature` no se envía si no se pide (Claude 5.x y GPT de razonamiento la rechazan); `anthropic-direct` usa structured outputs nativos (`output_config.format`); `openai-direct` usa `max_completion_tokens`.
  - `complete()` legado NO cambia (salvo heredar la reparación del registro): misma cascada, mismas etiquetas `*-fallback`, `usage_event=None`.
- El canary de NightWatch (task 15) vigila a diario que los modelos del registro sigan vivos. xAI queda `unmonitored` hasta provisionar key (Fase 2). Su lista NO se lee de aquí: vive en `nightwatch/config.yaml` (`tasks.15_model_canary.models`) del repo NightWatch y se sincroniza a mano con `_DEFAULTS` tras cada cambio del registro (la ola 6 lo cambia para que lea el registro).
- Origen del patrón (retirada silenciosa de gemini-2.0-flash): L-014 en `.ag/learnings.md`.

## Evals (`evals.py`)
- Golden sets JSONL (`{"id", "input", "expected"}`) + `run_golden(path, fn)` → accuracy y fallos.
- Convención para CONSUMIDORES con golden set propio: tests marcados `@pytest.mark.eval` llaman al LLM REAL → excluir en su CI con `pytest -m "not eval"`, correr aparte con `pytest -m eval -v` (con keys). Ejemplo real: `nickname-management-gmail` (`management_inbox_classification` en `.ag/contracts/governance.yaml`, `eval_gate: pytest -m eval ≥0.85`).
- Este repo NO tiene hoy tests `eval` propios ni marker registrado; su CI/`verify.sh` corren `pytest tests/` sin filtro `-m` (ver Testing). Si se añade un test `eval` aquí, hay que sumar `-m "not eval"` a `verify.yml`/`verify.sh` o correrá en CI contra un LLM real sin keys.
- Gate: cambio de modelo o prompt en un servicio con golden set debe pasar su eval antes de deploy.

## Observabilidad y presupuesto LLM (WI-1, SPEC-RAILWAY-AUTOMATION-MIGRATION-20260911)

Nace del incidente de agotamiento de límites por ejecuciones recurrentes sobre contexto
conversacional largo (supervisión de Room Service cada 15 min, >200k tokens por pasada).
Cuatro piezas, cada una en su propio módulo, cero dependencias externas:

- **`models/llm_usage.py`** — `AutomationRunV1` (una ejecución de un job: huella,
  si hubo novedad, si hizo falta LLM) y `LLMUsageEventV1` (una llamada real o
  bloqueada: proveedor, modelo, tokens, coste, motivo). Invariante DURO
  reforzado en `__post_init__` — no una convención: `cost_source="unavailable"`
  exige `cost_micros=None`; `cost_source` en `{provider_response, catalog}`
  exige `cost_micros` con valor. Construir uno inconsistente lanza
  `ValueError` en el momento, no produce un "0 gastado" silencioso más tarde.
  Ninguno de los dos modelos lleva PII, prompt ni respuesta — son metadatos
  de ejecución y gasto exclusivamente.
- **`budget.py`** — `BudgetGuard`: corte de gasto mensual fail-closed
  (`check_before_call()`) + auditoría post-hoc del tope por llamada
  (`check_call_cost()`, el coste real solo se conoce tras la respuesta) +
  registro (`record_call()`, un `cost_micros=None` NUNCA suma como 0). El
  estado de gasto acumulado lo persiste el CONSUMIDOR detrás del protocolo
  `BudgetStore` (Railway Volume, tabla, lo que tenga) — este módulo no sabe
  de ficheros ni de DBs, y no serializa nada por sí mismo: un store con
  escritura concurrente debe serializarse en el propio store (ver tests de
  concurrencia en `tests/test_budget.py` para el patrón con `threading.Lock`).
  Un store que lanza excepción o devuelve un negativo BLOQUEA
  (`BudgetBlockedReason.STORE_UNAVAILABLE`) — nunca se interpreta como
  presupuesto disponible. `BudgetStatus.store_error` (y `to_dict()["budget.
  readable"]`) distingue esa causa de un mes agotado de verdad: ambas dan
  `blocked=True`, pero solo una necesita arreglar el store en vez de esperar
  al mes que viene.
- **`cost_catalog.py`** — fallback de coste SOLO para cuando el proveedor no
  lo informa (p.ej. el leg `google-direct`, que da tokens pero nunca coste).
  El leg OpenRouter primario SÍ trae coste real vía `usage.include=True` y no
  pasa por aquí. Catálogo deliberadamente corto: `gemini-2.5-pro`
  (verificado 2026-08-25) y `gemini-2.5-flash-lite` (cargo real y readback
  del 2026-09-13, `docs/evidence/flash-lite-cost-20260913.json`). Flash-Lite
  cubre texto estándar sin caché ni herramientas; audio, prioridad, batch y
  descuentos no se infieren de ese precio. Añadir un
  modelo sin verificarlo así está prohibido — un precio no verificado que se
  usara para "cortar presupuesto" o "declarar ahorro" sería exactamente el
  antipatrón que la SPEC prohíbe en su §17. Modelo ausente → `None`, nunca un
  número inventado.
- **`fingerprint.py`** — `fingerprint(obj)` (SHA-256 de JSON canónico,
  estable ante reordenar claves de dict) e `idempotency_key(*parts)` (clave
  legible tipo `"feedback:2026-09-10"`, construida SIEMPRE con datos del
  periodo, nunca con la hora de ejecución). Es lo que permite "huella sin
  cambios → cero LLM" (checkers de delta) y "mismo periodo → no reprocesar"
  (informes diarios idempotentes).

Patrón de uso en código NUEVO (plano IA, ola 1) — `complete_for` hace el
presupuesto, el coste y el evento por ti:
```python
from nickname_common.budget import BudgetGuard, BudgetExhausted
from nickname_common.llm import complete_for, CascadeExhausted

guard = BudgetGuard(store=mi_store, month_limit_micros=LIMITE_MES, per_call_limit_micros=TOPE_LLAMADA)
try:
    res = complete_for("clasificacion_masiva", mensajes, guard=guard, json_schema=schema)
except BudgetExhausted as exc:
    ...  # conservar lo determinista; exc.usage_event / exc.result si los hay
except CascadeExhausted as exc:
    ...  # todos los pasos fallaron; exc.usage_event (transport_error)
guardar(res.usage_event.to_dict())
```

Patrón legado (consumidores de `complete()`), combinado a mano en un job:
```python
from nickname_common.fingerprint import fingerprint, idempotency_key
from nickname_common.budget import BudgetGuard, BudgetExhausted
from nickname_common.models import AutomationRunV1, LLMUsageEventV1, CostSource
from nickname_common import llm
from nickname_common.cost_catalog import estimate_cost_micros

huella = fingerprint({"sha": sha, "ci": ci_status})
if huella == huella_anterior:
    return  # AutomationRunV1(changed=False, llm_required=False, llm_calls=0)

try:
    guard.check_before_call()
except BudgetExhausted:
    return  # conservar resultado determinista, marcar outcome=budget_blocked

resultado = llm.complete("gemini_flash_lite", mensajes, json_schema=schema)
cost = resultado.usage.get("cost_micros")
cost_source = CostSource.PROVIDER_RESPONSE if cost is not None else CostSource.CATALOG
if cost is None:
    cost = estimate_cost_micros(resultado.model, input_tokens=..., output_tokens=...)
    cost_source = CostSource.CATALOG if cost is not None else CostSource.UNAVAILABLE
guard.record_call(cost)
```

## Env vars (del consumidor)
| Variable | Módulo | Default |
|---|---|---|
| `LOG_LEVEL` | logging | INFO |
| `ODOO_URL` / `ODOO_DB` / `ODOO_USERNAME` / `ODOO_API_KEY` | odoo_client | — |
| `ODOO_COMPANY_ID` | odoo_client | 2 |
| `ODOO_XMLRPC_TIMEOUT` / `ODOO_CIRCUIT_BREAKER_TIMEOUT` | odoo_client | 20s / 60s |
| `HUBSPOT_ACCESS_TOKEN` | hubspot_client | — |
| `NK_MODEL_<TIER>` | llm | defaults del registro |
| `XAI_API_KEY` | llm.complete | no provisionada (Fase 1) |
| `NK_XAI_LIVE` | llm.complete | off; `1` habilita HTTP live (Fase 2) |
| `NK_ROUTER` / `OPENROUTER_API_KEY` / `NK_AGENT_NAME` | llm.complete y complete_for (enrutador; `NK_AGENT_NAME` también es el `service` del evento de uso) | off; `openrouter` enruta |
| `GEMINI_API_KEY` (alias `GOOGLE_AI_API_KEY`) | llm.complete (leg gemini_directo) y complete_for (`gemini_direct`) | — (sin key, leg saltado) |
| `OPENAI_API_KEY` / `NK_MODEL_OPENAI_FALLBACK` | llm.complete (leg openai) y complete_for (`openai_direct`, modelo del tier) | — / `gpt-4o-mini` |
| `ANTHROPIC_API_KEY` / `NK_MODEL_ANTHROPIC_FALLBACK` | llm.complete (leg anthropic) y complete_for (`anthropic_direct`, solo agentes críticos de dinero por D-A) | — / `claude-haiku-4-5-20251001` |
| `AG_DECISIONS_LOG` | activity_logger | auto-detect |

## Testing
```bash
cd ~/Desktop/Apps/nickname-common
python3 -m pytest tests/ -q --tb=short   # unit (CI corre esto en PR, SIN filtro -m)
bash scripts/verify.sh                    # versión + install + tests + scan de secretos
```

## Consumidores
No mantener lista aquí — verla en vivo:
```bash
grep -rn "nickname-common" ~/Desktop/Apps/*/requirements*.txt                 # quién consume
grep -rn "nickname-common" ~/Desktop/Apps/*/requirements*.txt | grep "@main"  # quién NO pinea SHA
```
Patrón observado: los agentes locales sin Railway (Fiscal, LinkedIn) son los que más usan `@main` en vez de pin — menos presión de coordinar un bump. Lo que importa operativamente es el gotcha 4, no memorizar el listado exacto.

## Gotchas (solo de este repo)
1. **Circuit breaker Odoo**: tras un fallo de conexión/timeout (no de autenticación) rechaza llamadas durante 60s — no reintentar en caliente. Un fallo de auth (`OdooAuthenticationError`, ej. API key rotada) NO abre el circuito a propósito: falla rápido y explícito en vez de parecer una caída de red (ver `tests/test_odoo_client.py`).
2. **Redacción NO es default**: logs con payloads sensibles → `setup_logger_safe()` explícito.
3. **`company_id=2` es default hardcodeado** en `OdooService` — override por argumento o `ODOO_COMPANY_ID` (multi-company: ver `.ag/odoo_reference.md`).
4. **El pin manda**: mergear en main NO actualiza a nadie — sin bump de SHA en el agente, el fix no llega a producción.
