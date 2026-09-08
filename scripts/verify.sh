#!/usr/bin/env bash
set -euo pipefail
echo "=== Verify: nickname-common ==="

# Revisa el índice: también detecta copias añadidas con git add --force.
# La CI reutiliza sólo este guard, sin instalar ni ejecutar nada más.
python3 - <<'PY'
import re
import subprocess
import sys

paths = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
residual = re.compile(rb"(?:\.git|\.github|nickname_common|scripts) [0-9]+(?:/|$)")
count = sum(bool(residual.match(path)) for path in paths if path)
if count:
    print(f"FAIL: {count} archivos versionados en copias numeradas residuales")
    sys.exit(1)
print("OK: sin copias numeradas residuales versionadas")
PY
if [ "${1:-}" = "--check-residuals" ]; then
  exit 0
fi

# 1. Check Python
python3 --version

# 2. Install package in editable mode
if [ -z "${VIRTUAL_ENV:-}" ]; then
  echo "Warning: No venv active, installing deps..."
  pip3 install -e . -q
fi
pip3 install pytest -q

# 3. Run tests
echo "--- Running tests ---"
python3 -m pytest tests/ -q --tb=short

# 4. Check forbidden tokens (no hardcoded secrets)
echo "--- Checking forbidden tokens ---"
if grep -rn "ODOO_API_KEY\|HUBSPOT_ACCESS_TOKEN\|ANTHROPIC_API_KEY" --include="*.py" nickname_common/ 2>/dev/null | grep -v "os.getenv\|os.environ\|\.env\|config\." | head -5; then
  echo "FAIL: Possible hardcoded secrets found!"
  exit 1
fi

echo "OK: All checks passed"
