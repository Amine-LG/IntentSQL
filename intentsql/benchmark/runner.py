"""Repeatable benchmark runner for any NL->SQL system.

A *system* is a callable ``(db_path, question) -> Prediction``.  The runner
executes reference and predicted SQL, scores execution equivalence, classifies
failures and aggregates usage, latency and stability statistics.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from intentsql.benchmark.analysis import diagnose
from intentsql.benchmark.exec_match import (execute, order_matters, results_equivalent,
                                            set_equivalent)
from intentsql.benchmark.spider import Example, primary_category

ANSWERED, REFUSED, AMBIGUOUS, ERROR = "answered", "refused", "ambiguous", "error"


@dataclass
class Prediction:
    status: str
    sql: str | None = None
    params: tuple[Any, ...] = ()
    message: str | None = None
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    latency_ms: int = 0
    trace: Any = None


System = Callable[[Path, str], Prediction]


@dataclass
class CaseResult:
    key: str
    db_id: str
    hardness: str
    category: str
    tags: tuple[str, ...]
    status: str
    correct: bool
    set_correct: bool
    executable: bool
    errors: list[str] = field(default_factory=list)
    question: str | None = None
    reference_sql: str | None = None
    predicted_sql: str | None = None
    params: list[Any] | None = None
    message: str | None = None
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    latency_ms: int = 0
    trace: Any = None


def evaluate_case(example: Example, prediction: Prediction, *, redact: bool = False,
                  keep_trace: bool = False) -> CaseResult:
    reference = execute(example.db_path, example.query)
    correct = set_correct = executable = False
    errors: list[str] = []
    if reference.error:
        errors.append("reference_error")
    if prediction.status == ANSWERED and prediction.sql:
        predicted = execute(example.db_path, prediction.sql, prediction.params)
        executable = predicted.error is None
        if not executable:
            errors.append("sql_error")
        elif reference.rows is not None and predicted.rows is not None:
            correct = results_equivalent(reference.rows, predicted.rows,
                                         order_matters=order_matters(example.query))
            set_correct = correct or set_equivalent(reference.rows, predicted.rows)
            if not correct:
                errors.extend(diagnose(example.query, prediction.sql))
    elif prediction.status != ANSWERED:
        errors.append(prediction.status)
    return CaseResult(
        key=example.key, db_id=example.db_id, hardness=example.hardness,
        category=primary_category(example.tags), tags=example.tags,
        status=prediction.status, correct=correct, set_correct=set_correct,
        executable=executable, errors=errors,
        question=None if redact else example.question,
        reference_sql=None if redact else example.query,
        predicted_sql=None if redact else prediction.sql,
        params=None if redact else list(prediction.params),
        message=None if redact else prediction.message,
        jev_calls=prediction.jev_calls, input_tokens=prediction.input_tokens,
        output_tokens=prediction.output_tokens, retries=prediction.retries,
        latency_ms=prediction.latency_ms,
        trace=prediction.trace if keep_trace and not redact else None)


def _safe_predict(system: System, example: Example) -> Prediction:
    started = time.monotonic()
    try:
        prediction = system(example.db_path, example.question)
    except Exception as exc:  # A crash is a recorded system error, not a runner failure.
        prediction = Prediction(ERROR, message=f"{type(exc).__name__}: {exc}")
    if not prediction.latency_ms:
        prediction.latency_ms = round((time.monotonic() - started) * 1000)
    return prediction


def run(system: System, examples: Sequence[Example], *, workers: int = 8,
        redact: bool = False, keep_trace: bool = False,
        progress: Callable[[int, int, CaseResult], None] | None = None) -> list[CaseResult]:
    results: list[CaseResult | None] = [None] * len(examples)

    def work(index: int) -> None:
        example = examples[index]
        case = evaluate_case(example, _safe_predict(system, example), redact=redact,
                             keep_trace=keep_trace)
        results[index] = case
        if progress:
            progress(sum(r is not None for r in results), len(examples), case)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, range(len(examples))))
    return [r for r in results if r is not None]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarize(cases: Iterable[CaseResult]) -> dict[str, Any]:
    cases = list(cases)
    n = len(cases)
    answered = [c for c in cases if c.status == ANSWERED]
    correct = sum(c.correct for c in cases)
    summary: dict[str, Any] = {
        "cases": n,
        "execution_accuracy": _rate(correct, n),
        "set_accuracy": _rate(sum(c.set_correct for c in cases), n),
        "answered_rate": _rate(len(answered), n),
        "accuracy_when_answered": _rate(correct, len(answered)),
        "wrong_answer_rate": _rate(sum(not c.correct for c in answered), n),
        "refused": sum(c.status == REFUSED for c in cases),
        "ambiguous": sum(c.status == AMBIGUOUS for c in cases),
        "system_errors": sum(c.status == ERROR for c in cases),
        "sql_valid_rate": _rate(sum(c.executable for c in answered), len(answered)),
        "error_categories": dict(Counter(e for c in cases if not c.correct for e in c.errors)
                                 .most_common()),
        "jev_calls_mean": round(statistics.mean(c.jev_calls for c in cases), 2) if n else None,
        "input_tokens_mean": round(statistics.mean(c.input_tokens for c in cases)) if n else None,
        "output_tokens_mean": round(statistics.mean(c.output_tokens for c in cases)) if n else None,
        "retries_total": sum(c.retries for c in cases),
        "latency_ms_p50": _percentile([c.latency_ms for c in cases], 0.5),
        "latency_ms_p95": _percentile([c.latency_ms for c in cases], 0.95),
    }
    for field_name in ("hardness", "category"):
        groups: dict[str, list[CaseResult]] = defaultdict(list)
        for case in cases:
            groups[getattr(case, field_name)].append(case)
        summary[f"by_{field_name}"] = {
            key: {"cases": len(group),
                  "execution_accuracy": _rate(sum(c.correct for c in group), len(group)),
                  "answered": sum(c.status == ANSWERED for c in group),
                  "accuracy_when_answered": _rate(
                      sum(c.correct for c in group),
                      sum(c.status == ANSWERED for c in group))}
            for key, group in sorted(groups.items())}
    return summary


def batch_statistics(batch_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean / standard deviation / range of key metrics across random batches."""
    stats: dict[str, Any] = {"batches": len(batch_summaries)}
    for metric in ("execution_accuracy", "accuracy_when_answered", "answered_rate",
                   "wrong_answer_rate"):
        values = [s[metric] for s in batch_summaries if s.get(metric) is not None]
        if values:
            stats[metric] = {
                "mean": round(statistics.mean(values), 4),
                "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
                "min": min(values), "max": max(values)}
    return stats


def write_report(path: Path, meta: dict[str, Any], cases: list[CaseResult],
                 summary: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "summary": summary, **(extra or {}),
               "cases": [asdict(c) for c in cases]}
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
