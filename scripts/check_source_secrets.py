#!/usr/bin/env python3
"""Guard estático para fuentes Python del paquete; no importa ni ejecuta esas fuentes.

Detecta formatos de proveedor y literales en campos de credenciales, incluidos
defaults. Las menciones de nombres de variables no son valores. No analiza flujo
de datos ni descifra secretos ofuscados. No hay exclusiones de archivos o líneas.
La salida usa índices de archivo: tampoco refleja rutas controladas en el log.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys


# Prefijo + material de clave: las menciones como rk_live_* no coinciden (L-073).
PROVIDER = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"sk-(?:ant-|proj-|svcacct-|or-v1-)?[A-Za-z0-9_-]{20,}"
    r"|pat-[A-Za-z0-9]{3,}-[A-Za-z0-9-]{20,}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|xai-[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|(?:rk|sk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,}"
    r"|xkeysib-[A-Za-z0-9-]{30,}|shpat_[A-Fa-f0-9]{32}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r")"
)
SENSITIVE = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|auth_?token|refresh_?token|"
    r"client_?secret|secret_?key|password|passwd|pwd|secret|token|authorization)$"
)


def sensitive(name: str | None) -> bool:
    if not name:
        return False
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower().replace("-", "_")
    return SENSITIVE.search(normalized) is not None


def literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = literal(node.left), literal(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr) and all(isinstance(v, ast.Constant) for v in node.values):
        return "".join(v.value for v in node.values)
    return None


def target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return literal(node.slice)
    return None


def scan_source(source: str) -> list[tuple[int, str]]:
    findings = {(n, "provider_value") for n, line in enumerate(source.splitlines(), 1)
                if PROVIDER.search(line)}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return sorted(findings | {(0, "source_parse_error")})

    def credential(name, value):
        if not sensitive(name):
            return
        if isinstance(value, ast.BoolOp):
            for candidate in value.values:
                credential(name, candidate)
        elif isinstance(value, ast.IfExp):
            credential(name, value.body)
            credential(name, value.orelse)
        elif literal(value):
            findings.add((value.lineno, "literal_credential"))

    def assignment(target, value):
        if isinstance(value, ast.IfExp):
            assignment(target, value.body)
            assignment(target, value.orelse)
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            if len(target.elts) == len(value.elts):
                for field, item in zip(target.elts, value.elts):
                    assignment(field, item)
        else:
            credential(target_name(target), value)

    for node in ast.walk(tree):
        value = literal(node)
        if value and PROVIDER.search(value):
            findings.add((node.lineno, "provider_value"))
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                assignment(target, node.value)
        elif isinstance(node, ast.Dict):
            for key, value_node in zip(node.keys, node.values):
                credential(literal(key), value_node)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                credential(keyword.arg, keyword.value)
            # No se excluye la línea por mencionar env: un default literal falla.
            function = node.func.attr if isinstance(node.func, ast.Attribute) else target_name(node.func)
            if function in {"get", "getenv", "setdefault"} and node.args:
                name = literal(node.args[0])
                if len(node.args) > 1:
                    credential(name, node.args[1])
                for keyword in node.keywords:
                    if keyword.arg == "default":
                        credential(name, keyword.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args.posonlyargs + node.args.args
            for arg, default in zip(args[-len(node.args.defaults):], node.args.defaults):
                credential(arg.arg, default)
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                credential(arg.arg, default)
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("FAIL: scan_scope_invalid")
        return 2
    try:
        root = Path(args[0])
        paths = sorted(root.rglob("*.py")) if root.is_dir() else [root]
        if not paths:
            raise OSError
        results = []
        for index, path in enumerate(paths, 1):
            findings = scan_source(path.read_text(encoding="utf-8"))
            results.extend((index, line, reason) for line, reason in findings)
    except (OSError, UnicodeError):
        print("FAIL: scan_scope_unavailable")
        return 2
    for index, line, reason in results:
        print(f"source[{index}]:{line}:<redacted> [{reason}]")
    if results:
        return 1
    print(f"OK: {len(paths)} fuentes Python sin credenciales literales detectadas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
