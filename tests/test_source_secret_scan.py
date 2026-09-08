"""Pruebas sintéticas del verificador; ningún token se consulta a un proveedor."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_source_secrets.py"
spec = importlib.util.spec_from_file_location("source_secret_scan", SCRIPT)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


@pytest.mark.parametrize("source", [
    '"""Lee HUBSPOT_ACCESS_TOKEN de env; ODOO_API_KEY es un nombre."""',
    'raise ValueError("HUBSPOT_ACCESS_TOKEN no configurado")',
    'api_key = os.getenv("ODOO_API_KEY")',
    'token = os.environ["HUBSPOT_ACCESS_TOKEN"]',
    'token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")',
    'self.api_key = api_key or os.getenv("ODOO_API_KEY")',
    'Client(api_key=config.api_key)',
    'headers = {"Authorization": f"Bearer {token}"}',
    'def connect(api_key=None, *, password=""): pass',
    'ODOO_API_KEY, label = os.getenv("ODOO_API_KEY"), "not a credential"',
    '[label, token] = ["not a credential", os.environ["TOKEN"]]',
    '(label, (token, count)) = ("label", (os.getenv("TOKEN"), 1))',
    'ODOO_API_KEY = os.getenv("ODOO_API_KEY") if enabled else ""',
    'token, label = (None, "label") if enabled else (os.getenv("TOKEN"), "label")',
    '# Documentación de familias: rk_live_*, sk_live_*, whsec_*, xkeysib-*',
])
def test_mentions_and_runtime_environment_are_not_credentials(source):
    assert scanner.scan_source(source) == []


@pytest.mark.parametrize("source", [
    'ODOO_API_KEY = SAMPLE',
    'self.api_key: str = SAMPLE',
    'settings["password"] = SAMPLE',
    'headers = {"Authorization": SAMPLE}',
    'Client(api_key=SAMPLE)',
    'config = {"client_secret": SAMPLE}',
    'apiKey = PART1 + PART2',
    'password = fSAMPLE',
    'token = os.getenv("TOKEN", SAMPLE)',
    'token = os.environ.get("TOKEN", default=SAMPLE)',
    'token = os.getenv("TOKEN") or SAMPLE',
    'os.environ.setdefault("TOKEN", SAMPLE)',
    'def connect(password=SAMPLE): pass',
    'def connect(*, api_key=SAMPLE): pass',
    'token = SAMPLE  # os.getenv y config. no excluyen esta línea',
    'ODOO_API_KEY, other = SAMPLE, None',
    '[other, password] = [None, SAMPLE]',
    'other, (token, count) = "label", (SAMPLE, 1)',
    'ODOO_API_KEY = SAMPLE if enabled else ""',
    'ODOO_API_KEY = "" if enabled else SAMPLE',
    'token, label = (SAMPLE, "label") if enabled else (None, "label")',
    'token = os.getenv("TOKEN") or (SAMPLE if enabled else "")',
])
def test_literal_credentials_are_detected_without_line_exemptions(source):
    # Construye fuentes sintéticas en RAM; no añade excepciones al scanner del repo.
    source = source.replace("SAMPLE", repr("fixture-" + "value"))
    source = source.replace("PART1", repr("fixture-")).replace("PART2", repr("value"))
    assert any(reason == "literal_credential" for _, reason in scanner.scan_source(source))


@pytest.mark.parametrize("prefix,tail", [
    ("sk-", "A" * 48), ("sk-ant-", "A" * 48),
    ("sk-proj-", "A" * 48), ("sk-or-v1-", "A" * 48),
    ("pat-eu1-", "A" * 48), ("AIza", "A" * 35),
    ("xai-", "A" * 48), ("ghp_", "A" * 36),
    ("rk_live_", "A" * 32), ("sk_live_", "A" * 32),
    ("whsec_", "A" * 32), ("xkeysib-", "A" * 48),
    ("shpat_", "a" * 32),
])
def test_provider_values_detected_even_in_documentation(prefix, tail):
    value = prefix + tail
    assert scanner.scan_source(f'"""No debe publicarse: {value}"""')
    assert scanner.scan_source(f'other_name = {prefix!r} + {tail!r}')


def test_cli_redacts_values_paths_and_parse_errors(tmp_path):
    value = "sk-" + "A" * 48
    source = tmp_path / (value + ".py")
    for text in [f'key = "{value}"', f'broken({value}']:
        source.write_text(text)
        run = subprocess.run([sys.executable, str(SCRIPT), str(source)], capture_output=True, text=True)
        assert run.returncode == 1
        assert "<redacted>" in run.stdout
        assert value not in run.stdout + run.stderr
        assert str(source) not in run.stdout + run.stderr


def test_cli_rejects_missing_or_empty_scope(tmp_path, capsys):
    assert scanner.main([str(tmp_path / "missing.py")]) == 2
    assert scanner.main([str(tmp_path)]) == 2
    assert "FAIL:" in capsys.readouterr().out


def test_cli_accepts_environment_reference(tmp_path):
    (tmp_path / "client.py").write_text('api_key = os.getenv("ODOO_API_KEY")')
    assert scanner.main([str(tmp_path)]) == 0


def test_repository_package_passes_source_guard():
    # La CI existente ejecuta pytest: comprueba también las fuentes reales,
    # además de las muestras sintéticas. No importa ni ejecuta el paquete.
    assert scanner.main([str(SCRIPT.parents[1] / "nickname_common")]) == 0
