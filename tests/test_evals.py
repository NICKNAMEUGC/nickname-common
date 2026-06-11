"""Tests del eval harness (F0.4)."""

from nickname_common.evals import EvalReport, load_golden, run_golden


def _write_golden(tmp_path, lines):
    p = tmp_path / "golden.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_load_golden_skips_comments_and_blanks(tmp_path):
    p = _write_golden(tmp_path, [
        '# comentario',
        '',
        '{"id": "a", "input": "hola", "expected": "noise"}',
        '{"input": "sin id", "expected": "x"}',
    ])
    cases = load_golden(p)
    assert len(cases) == 2
    assert cases[0].id == "a"
    assert cases[1].id == "line-4"  # id autogenerado por línea


def test_run_golden_accuracy_and_failures(tmp_path):
    p = _write_golden(tmp_path, [
        '{"id": "ok", "input": "a", "expected": "A"}',       # pasa (case-insensitive)
        '{"id": "ko", "input": "b", "expected": "noise"}',   # falla
    ])
    report = run_golden(p, lambda x: x.upper())
    assert report.total == 2
    assert report.passed == 1
    assert report.accuracy == 0.5
    assert report.failures[0].id == "ko"
    assert "FAIL ko" in report.summary()


def test_run_golden_exception_counts_as_failure(tmp_path):
    p = _write_golden(tmp_path, ['{"id": "boom", "input": 1, "expected": 1}'])

    def fn(x):
        raise ValueError("explota")

    report = run_golden(p, fn)
    assert report.accuracy == 0.0
    assert "ValueError" in report.failures[0].error


def test_run_golden_custom_compare(tmp_path):
    p = _write_golden(tmp_path, ['{"id": "c", "input": 5, "expected": 4}'])
    report = run_golden(p, lambda x: x, compare=lambda exp, act: abs(exp - act) <= 1)
    assert report.accuracy == 1.0


def test_empty_report():
    assert EvalReport().accuracy == 0.0
