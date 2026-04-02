<!-- DOC-TYPE: AUTORITATIVO -->
# nickname-common

Libreria Python compartida por todos los agentes del ecosistema Nickname. Zero dependencias externas (solo stdlib). Logging, config, health checks, clientes Odoo/HubSpot, activity logging, modelos de datos.

## Reglas
- Python 3.12+, sin dependencias externas (solo stdlib)
- NO tiene .env propio — hereda del repo que lo importa
- Cambios aqui afectan a TODOS los agentes — testear antes de push
- ODOO_COMPANY_ID=2 siempre (NICKNAME MANAGEMENT SL.)
- Thread-safe en todos los modulos (RLock, file locking)

## Instalacion
```bash
# Desarrollo local (editable)
pip install -e ~/Desktop/Apps/nickname-common

# En requirements.txt de agentes (SHA pinned)
nickname-common @ git+https://github.com/NICKNAMEUGC/nickname-common.git@fe17ba54fd412240e325b3d2d60fb738b3044ca1
```

## Estructura
```
nickname-common/
├── nickname_common/
│   ├── __init__.py               # Exports: setup_logger, load_config, DeepHealthChecker, etc.
│   ├── logging.py                # setup_logger() + setup_logger_safe() (con redaccion)
│   ├── config.py                 # load_config(required=[], optional={})
│   ├── health.py                 # DeepHealthChecker (parallel threading, timeout)
│   ├── log_redactor.py           # RedactingFilter + redact() — enmascara API keys/tokens
│   ├── odoo_client.py            # OdooService — XML-RPC con circuit breaker + lazy init
│   ├── hubspot_client.py         # HubSpotService — REST con rate limiting + auto-retry
│   ├── activity_logger.py        # ActivityLogger — escribe decisions_log.md (file locking)
│   └── models/
│       ├── task.py               # Task, TaskStatus, TaskPriority, TasksResponse
│       ├── health.py             # HealthCheck, HealthResponse, ServiceStatus
│       ├── automation.py         # AutomationJob, AutomationSeverity, AutomationsResponse
│       └── activity.py           # ActivityEntry, ActivityLevel
│
├── tests/
│   ├── test_logging.py
│   ├── test_config.py
│   ├── test_health.py
│   ├── test_log_redactor.py
│   ├── test_activity_logger.py
│   └── test_models.py
│
├── scripts/
│   └── verify.sh                 # Pre-push: version check, install, tests, secret scan
├── setup.py                      # Package config (nickname-common 0.1.0)
└── ruff.toml                     # Linter config
```

## API publica (imports)

```python
from nickname_common import (
    setup_logger,             # Logger estandarizado [SERVICE] [LEVEL] msg
    setup_logger_safe,        # Igual pero con RedactingFilter incluido
    load_config,              # Validacion env vars (required + optional con defaults)
    DeepHealthChecker,        # Health checks paralelos con threading
    RedactingFilter,          # logging.Filter que enmascara secrets
    redact,                   # Funcion standalone para redactar strings
)

from nickname_common.odoo_client import OdooService
from nickname_common.hubspot_client import HubSpotService
from nickname_common.activity_logger import ActivityLogger

from nickname_common.models import (
    Task, TaskStatus, TaskPriority, TasksResponse,
    HealthCheck, HealthResponse, ServiceStatus,
    AutomationJob, AutomationSeverity, AutomationsResponse,
    ActivityEntry, ActivityLevel,
)
```

## Modulos detallados

### logging.py
```python
logger = setup_logger("email-marketing")        # [EMAIL-MARKETING] [INFO] msg
logger = setup_logger_safe("email-marketing")    # Igual + RedactingFilter
```
- Respeta LOG_LEVEL env var (default: INFO)
- Caching: reutiliza loggers por nombre, no duplica handlers

### config.py
```python
config = load_config(
    required=["ODOO_URL", "HUBSPOT_ACCESS_TOKEN"],
    optional={"RETRY_ATTEMPTS": "3", "LOG_LEVEL": "INFO"}
)
# Lanza ValueError si falta alguna required
```

### health.py — DeepHealthChecker
```python
checker = DeepHealthChecker("my-service", version="1.0.0")
checker.add_check("odoo", lambda: odoo.test_connection(), timeout_sec=10)
checker.add_check("hubspot", lambda: hs.test_connection())
result = checker.run()  # Dict con status: online/degraded/offline
```

### odoo_client.py — OdooService
```python
odoo = OdooService()  # Lee ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY
odoo.search("res.partner", [("is_company", "=", True)])
odoo.search_read("crm.lead", [("stage_id.name", "=", "Won")], fields=["name", "expected_revenue"])
odoo.create("res.partner", {"name": "Test", "company_id": 2})
odoo.write("res.partner", [123], {"phone": "+34..."})
odoo.test_connection()  # Dict con status, uid, error
```
- **Circuit breaker**: Si Odoo falla, rechaza calls por 60s (configurable)
- **Lazy init**: Autenticacion al primer uso
- **Thread-safe**: RLock serializa llamadas

### hubspot_client.py — HubSpotService
```python
hs = HubSpotService()  # Lee HUBSPOT_ACCESS_TOKEN
hs.search_all("contacts", {"filterGroups": [...]})  # Paginacion automatica
hs.search_modified("deals", since_ms, properties=["dealname", "amount"])
hs.get_associations("deals", "companies", [deal_id])
hs.test_connection()
```
- **Rate limiting**: Auto-retry en 429 con exponential backoff (10s x attempt, max 3)

### activity_logger.py — ActivityLogger
```python
logger = ActivityLogger()  # Auto-detecta .ag/decisions_log.md
logger.log(task_id="T-123", agent="CTO", system="odoo-agent",
           flow="Flujo 2", event="Sync completado", decision="OK")
logger.log_quick(agent="orchestrator", message="Health check passed")
```
- File locking (fcntl) + threading.Lock para concurrencia segura

### log_redactor.py
```python
from nickname_common import redact
safe_text = redact("key is sk-ant-api03-xyz...")  # "key is ***ANTHROPIC_KEY***"
```
Patrones: Anthropic, OpenAI, Google, HubSpot keys, Bearer tokens, passwords, emails.

## Env vars (del consumidor)

| Variable | Modulo | Default |
|---|---|---|
| `LOG_LEVEL` | logging.py | INFO |
| `ODOO_URL` | odoo_client | — |
| `ODOO_DB` | odoo_client | — |
| `ODOO_USERNAME` | odoo_client | — |
| `ODOO_API_KEY` | odoo_client | — |
| `ODOO_COMPANY_ID` | odoo_client | 2 |
| `ODOO_XMLRPC_TIMEOUT` | odoo_client | 20s |
| `ODOO_CIRCUIT_BREAKER_TIMEOUT` | odoo_client | 60s |
| `HUBSPOT_ACCESS_TOKEN` | hubspot_client | — |
| `AG_DECISIONS_LOG` | activity_logger | Auto-detect |

## Testing
```bash
cd ~/Desktop/Apps/nickname-common
python3 -m pytest tests/ -q --tb=short

# Verificacion completa (version, install, tests, secrets)
bash scripts/verify.sh
```

## Usado por (8+ agentes)
email-marketing-agent, odoo-agent, paid-media-agent, emilia-gmail, management-gmail, linkedin-agent, analytics-dashboard, clickup-agent, dashboard.

## Gotchas
1. **Zero deps externas** — solo stdlib. Los agentes traen sus propias deps (Flask, etc.)
2. **company_id=2 OBLIGATORIO** — Hardcoded default en OdooService
3. **Circuit breaker Odoo** — Rechaza calls 60s tras fallo. No reintentar inmediatamente.
4. **Redaccion NO es default** — Usar `setup_logger_safe()` explicitamente
5. **SHA pinned** — Los agentes referencian un commit especifico. Actualizar SHA tras cambios.

## GitHub
https://github.com/NICKNAMEUGC/nickname-common (private)
