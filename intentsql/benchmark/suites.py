"""Hand-written evaluation suites (benchmarks/suites/*.json).

A case lists one or more acceptable reference queries, or expects a refusal.
Paraphrase variants are expanded into separate cases of one group; a group
passes only when every phrasing is answered correctly.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from intentsql.benchmark.exec_match import execute, order_matters, results_equivalent
from intentsql.benchmark.runner import ANSWERED, ERROR, REFUSED, Prediction, System
from intentsql.config import PROJECT_ROOT

SEARCH = (PROJECT_ROOT / "benchmarks" / "fixtures", PROJECT_ROOT / "data")


@dataclass
class SuiteCase:
    id: str
    database: Path
    question: str
    gold: list[str]
    ordered: bool
    expect: str                 # "answer" | "refuse"
    category: str
    group: str | None = None


@dataclass
class SuiteResult:
    case: SuiteCase
    status: str
    correct: bool
    sql: str | None = None
    message: str | None = None
    confidence: float | None = None
    calls: int = 0
    input_tokens: int = 0
    latency_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _database(name: str) -> Path:
    for directory in SEARCH:
        if (directory / name).is_file():
            return directory / name
    raise FileNotFoundError(name)


def load_suite(path: str | Path, *, include_paraphrases: bool = True) -> list[SuiteCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = []
    for raw in data["cases"]:
        gold = raw.get("gold_sql")
        golds = gold if isinstance(gold, list) else ([gold] if gold else [])
        expect = raw.get("expect", "answer")
        if expect == "answer" and not golds:
            continue                     # no reference: nothing to score
        category = raw.get("category") or raw.get("family") or data.get("suite", "suite")
        group = raw.get("group")
        base = SuiteCase(raw["id"], _database(raw["database"]), raw["question"], golds,
                         bool(raw.get("ordered")), expect, category, group)
        cases.append(base)
        if include_paraphrases:
            for i, text in enumerate(raw.get("paraphrases") or [], start=1):
                cases.append(SuiteCase(f"{raw['id']}-p{i}", base.database, text, golds, base.ordered,
                                       expect, category, group or raw["id"]))
                base.group = base.group or raw["id"]
    return cases


def score(case: SuiteCase, prediction: Prediction) -> SuiteResult:
    result = SuiteResult(case, prediction.status, False, prediction.sql, prediction.message,
                         prediction.confidence, prediction.jev_calls, prediction.input_tokens,
                         prediction.latency_ms)
    if case.expect == "refuse":
        result.correct = prediction.status == REFUSED
        return result
    if prediction.status != ANSWERED or not prediction.sql:
        return result
    mine = execute(case.database, prediction.sql, prediction.params)
    if mine.rows is None:
        result.extra["error"] = mine.error
        return result
    for gold in case.gold:
        reference = execute(case.database, gold)
        ordered = case.ordered or order_matters(gold)
        if reference.rows is not None and results_equivalent(reference.rows, mine.rows,
                                                             order_matters=ordered):
            result.correct = True
            break
    return result


def run_suite(system: System, cases: list[SuiteCase], *, workers: int = 6) -> list[SuiteResult]:
    def work(case: SuiteCase) -> SuiteResult:
        started = time.monotonic()
        try:
            prediction = system(case.database, case.question)
        except Exception as exc:     # recorded, not raised
            prediction = Prediction(ERROR, message=f"{type(exc).__name__}: {exc}")
        prediction.latency_ms = prediction.latency_ms or round((time.monotonic() - started) * 1000)
        return score(case, prediction)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(work, cases))


def summarize_suite(results: list[SuiteResult]) -> dict[str, Any]:
    by_category: dict[str, list[SuiteResult]] = defaultdict(list)
    groups: dict[str, list[SuiteResult]] = defaultdict(list)
    for r in results:
        by_category[r.case.category].append(r)
        if r.case.group:
            groups[r.case.group].append(r)
    answerable = [r for r in results if r.case.expect == "answer"]
    answered = [r for r in answerable if r.status == ANSWERED]
    refusals = [r for r in results if r.case.expect == "refuse"]
    return {
        "cases": len(results),
        "accuracy": round(sum(r.correct for r in results) / len(results), 4) if results else None,
        "answerable_accuracy": round(sum(r.correct for r in answerable) / len(answerable), 4)
        if answerable else None,
        "accuracy_when_answered": round(sum(r.correct for r in answered) / len(answered), 4)
        if answered else None,
        "answered_rate": round(len(answered) / len(answerable), 4) if answerable else None,
        "correct_refusals": f"{sum(r.correct for r in refusals)}/{len(refusals)}",
        "false_refusals": sum(r.status == REFUSED for r in answerable),
        "paraphrase_groups_consistent": f"{sum(all(x.correct for x in g) for g in groups.values())}/{len(groups)}",
        "by_category": {k: f"{sum(r.correct for r in v)}/{len(v)}" for k, v in sorted(by_category.items())},
        "jev_calls_mean": round(sum(r.calls for r in results) / len(results), 2) if results else None,
        "latency_ms_p50": sorted(r.latency_ms for r in results)[len(results) // 2] if results else None,
    }
