# Revisión de nickname-common #3

Fecha: 2026-11-14. WorkItem: `WI-COMMON-PR3-REVIEW-20261114`.

Veredicto: **requiere cambios; la PR original está superada por código integrado**.
La corrección vigente se prepara sobre `main` para revisión en una PR nueva.

## Alcance y fuentes

- [PR #3](https://github.com/NICKNAMEUGC/nickname-common/pull/3),
  HEAD `8cdbaf8aeab6354e73fc97016ef982bd917914f3`: lectura de los cuatro archivos
  cambiados, pruebas, discusión y resultados de GitHub Actions.
- Base de la corrección: `881072e1d292189090dbb8beb4927fb3331dfcae`.
- [PR #4, integrada](https://github.com/NICKNAMEUGC/nickname-common/pull/4),
  commit `fa067958186fe4d07dcf46c7e4d5553e84ddd633`: ya contiene los tiers Grok,
  el adaptador xAI y el guard de familia propuestos por #3.

`[D]` indica observación directa; `[I]`, una consecuencia inferida.

## Incidencias

| Prioridad | Incidencia y escenario | Estado |
|---|---|---|
| P1 | El error HTTP de xAI copia hasta 200 caracteres del cuerpo de respuesta a `ProviderNotConfigured`. En #3: `nickname_common/llm.py:270-274`; en la base actual: `llm.py:746-750`. | Reproducida y corregida en esta entrega. |
| P2 | Una respuesta 200 con `choices: []` lanza `IndexError` en #3 (`llm.py:277`). | Reproducida en el commit original; resuelta en `main` por `_openai_style_text`. No requiere otro cambio. |
| Bloqueo de integración | GitHub devuelve `mergeable=false`, `mergeable_state=dirty` para #3. Su funcionalidad ya está incorporada por #4. | Recomiendo cerrar #3 como superada tras revisar esta entrega. |
| Configuración de revisión | El comentario de `claude[bot]` informa que Claude Code Review está pausado. | Pendiente de revinculación administrativa si se quiere recuperar ese revisor. |

### P1: contenido de respuesta dentro de excepciones

[D] Un transporte inyectado que devuelve HTTP 400, 401, 429 o 503 y un cuerpo
sintético privado produce una excepción que contiene ese cuerpo. Las cuatro
regresiones fallan antes del cambio y pasan después. La excepción conserva el
proveedor, endpoint y código HTTP; su tipo sigue siendo `ProviderNotConfigured`.

[I] Un consumidor que registre esa excepción puede persistir contenido privado
si el proveedor lo incluye en el error. No se ha observado una filtración real:
la reproducción utiliza exclusivamente datos sintéticos y transporte simulado.

## Clasificación de CI

[D] El [run original de Verify](https://github.com/NICKNAMEUGC/nickname-common/actions/runs/32664578847)
terminó con `success`, incluido el paso de pruebas. No hay comprobaciones fallidas
que reintentar en ese HEAD.

[D] Su única anotación es un aviso de Node.js 20 en `actions/checkout@v4` y
`actions/setup-python@v5`. La base actual ya usa `@v7` para ambas acciones mediante
las PR #8 y #9. No se modifica el workflow.

## Comprobación de la corrección

- Python 3.12.12 y pytest 9.2.0, en un entorno virtual aislado y con la versión
  declarada en `requirements-dev.txt`.
- Antes del cambio de producción: cuatro regresiones HTTP fallidas.
- `bash scripts/verify.sh`: **569 pruebas pasadas**, guard de copias residuales
  correcto y 20 fuentes Python sin credenciales literales detectadas.
- Los tests de acceso verifican que `_urllib_post` no se llama sin credencial,
  con el flag ausente, apagado o encendido; también conservan el caso con
  credencial sintética y acceso live deshabilitado.
- `git diff --check`: correcto.

La modificación de producción se limita al mensaje de error de xAI. Los tests
utilizan transportes simulados; no se hacen llamadas de pago ni se provisionan
credenciales. La entrega es para revisión; no incluye merge, despliegue ni cambio
de pins en consumidores.

El preflight del ecosistema señaló bloqueos de `globals` y `session_checkpoints`.
La revisión y las pruebas se ejecutan en un worktree aislado; esta entrega no
acredita preparación del runner ni estado de producción.
