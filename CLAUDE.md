<!-- DOC-TYPE: REFERENCIA -->
# nickname-common

Librería Python compartida por todos los agentes del ecosistema Nickname.

## Qué contiene
- `setup_logger()` — Logger estandarizado con formato europeo
- Utilidades comunes (formateo de números, fechas, etc.)
- Modelos compartidos

## Instalación
```
pip install -e ~/Desktop/Apps/nickname-common
```

## Uso
```python
from nickname_common.logging import setup_logger
logger = setup_logger(__name__)
```

## Reglas
- NO tiene .env propio — hereda del repo que lo importa
- Cambios aquí afectan a TODOS los agentes — testear antes de push
