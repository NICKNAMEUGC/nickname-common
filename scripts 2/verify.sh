#!/usr/bin/env bash
set -euo pipefail
echo "=== Verify: nickname-common ==="

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
