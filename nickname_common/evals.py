"""Eval harness mínimo para comportamiento LLM (F0.4 Quality Leap 2026-06).

Un *golden set* es un JSONL donde cada línea es un caso etiquetado:

    {"id": "caso-01", "input": {...}, "expected": "important"}

`run_golden()` ejecuta una función sobre cada caso y devuelve un informe con
accuracy y fallos detallados. Pensado para pytest con marker `eval` — los
evals llaman al LLM REAL (ese es el punto: miden modelo+prompt, no código),
así que NO corren en CI por defecto:

    # tests/evals/test_classifier_golden.py
    import pytest
    from nickname_common.evals import run_golden

    @pytest.mark.eval
    def test_classifier_golden():
        report = run_golden("tests/evals/golden_classifier.jsonl",
                            lambda case: classify(case["text"]))
        assert report.accuracy >= 0.90, report.summary()

    # Ejecutar:  pytest -m eval -v        (con API keys en el entorno)
    # CI normal: pytest -m "not eval"

Gate de uso: cualquier cambio de modelo o prompt en un servicio con golden
set DEBE pasar su eval antes de deploy. Es lo que permite actualizar modelos
sin miedo (la lección estructural de L-014).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class EvalCase:
    id: str
    input: Any
    expected: Any
    actual: Any = None
    passed: bool = False
    error: str = ""


@dataclass
class EvalReport:
    cases: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def failures(self) -> list:
        return [c for c in self.cases if not c.passed]

    def summary(self) -> str:
        lines = [f"Eval: {self.passed}/{self.total} ({self.accuracy:.0%})"]
        for c in self.failures:
            detail = c.error or f"expected={c.expected!r} actual={c.actual!r}"
            lines.append(f"  FAIL {c.id}: {detail}")
        return "\n".join(lines)


def load_golden(path) -> list:
    """Carga un golden set JSONL. Líneas vacías y comentarios (#) se ignoran."""
    cases = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d = json.loads(line)
        cases.append(EvalCase(id=str(d.get("id", f"line-{i}")),
                              input=d["input"], expected=d["expected"]))
    return cases


def run_golden(path, fn: Callable, compare: Callable = None) -> EvalReport:
    """Ejecuta `fn(case.input)` sobre cada caso del golden set.

    compare(expected, actual) -> bool opcional; default: igualdad (case-insensitive
    para strings). Las excepciones de fn cuentan como fallo, no rompen el run.
    """
    if compare is None:
        def compare(exp, act):
            if isinstance(exp, str) and isinstance(act, str):
                return exp.strip().lower() == act.strip().lower()
            return exp == act

    report = EvalReport(cases=load_golden(path))
    for case in report.cases:
        try:
            case.actual = fn(case.input)
            case.passed = compare(case.expected, case.actual)
        except Exception as e:  # el eval debe completarse aunque un caso explote
            case.error = f"{type(e).__name__}: {e}"
            case.passed = False
    return report
