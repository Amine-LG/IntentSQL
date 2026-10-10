"""End-to-end request handling: ground -> interpret -> build -> compile -> execute.

``answer`` is the single entry point used by the CLI, the web app and the
benchmarks.  It always returns an ``Answer``; failures are reported as a
status with a reason, never as a partially executed query.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from intentsql import ir
from intentsql.compiler import CompileError, compile_query
from intentsql.grounding import ground
from intentsql.intent import Intent, PlanError, build_query
from intentsql.interpret import Decision, Strategy, alternatives, interpret
from intentsql.jev import Exchange, JevClient, JevError, Usage, choice, noul, shared_client
from intentsql.render import condition as condition_text
from intentsql.render import describe
from intentsql.schema import load_schema

ANSWERED, AMBIGUOUS, REFUSED, ERROR = "answered", "ambiguous", "refused", "error"
MAX_ROWS = 1000
QUERY_TIMEOUT_S = 15.0
PREVIEW_ROWS = 200
LOW_CONFIDENCE = 0.5      # calibrated probability under which a reading is reported as uncertain
ALTERNATIVE_PENALTY = 0.2  # each result-distinct alternative reading lowers the score (docs/EXPERIMENTS.md E12)
# Calibration: (score lower bound, observed accuracy) by isotonic regression of the score
# (verification probability - 0.2 x alternatives) against execution accuracy on Spider
# *train* runs (2,100 predictions, 2026-10-10).  Dev and test were not used.
# Calibration tables are per provider: a table measured with one model is not reused for
# another (an uncalibrated provider reports its raw verification score instead).
UNCALIBRATED_UNCERTAIN = 0.2   # raw verification under which an uncalibrated answer is uncertain
CALIBRATION = ((0.0, 0.33), (0.12, 0.42), (0.19, 0.59), (0.25, 0.64), (0.34, 0.71), (0.44, 0.76),
               (0.62, 0.84), (0.70, 0.88), (0.86, 0.94), (0.90, 0.97))
SWITCH_MARGIN = 0.25      # an alternative replaces the main reading only by this probability margin
SIMPLE_CONDITIONS = ("compare", "text", "between", "in_values", "null", "period")
VERIFY_QUESTION = ("Does `interpretation` describe exactly what `request` asks for, with nothing "
                   "missing, nothing extra and the same conditions? `result` shows what it returned.")


@dataclass
class Answer:
    status: str
    question: str
    sql: str | None = None
    params: tuple[Any, ...] = ()
    display_sql: str | None = None
    explanation: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    truncated: bool = False
    message: str | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    intent: dict[str, Any] | None = None
    confidence: float | None = None       # calibrated probability (None: provider not calibrated)
    provider: str = ""
    verification: float | None = None     # raw verification probability of the chosen reading
    decisions: list[Decision] = field(default_factory=list)
    exchanges: list[Exchange] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    timings_ms: dict[str, int] = field(default_factory=dict)

    def to_dict(self, *, debug: bool = True) -> dict[str, Any]:
        data = {
            "status": self.status, "question": self.question, "sql": self.sql,
            "params": list(self.params), "display_sql": self.display_sql,
            "explanation": self.explanation, "columns": self.columns,
            "rows": [list(r) for r in self.rows], "truncated": self.truncated,
            "message": self.message, "alternatives": self.alternatives,
            "confidence": self.confidence, "verification": self.verification, "provider": self.provider,
            "usage": self.usage.as_dict(), "timings_ms": self.timings_ms,
        }
        if debug:
            data["plan"] = self.plan
            data["intent"] = self.intent
            data["decisions"] = [asdict(d) for d in self.decisions]
            data["exchanges"] = [asdict(e) for e in self.exchanges]
        return data


def execute_read(db_path: str | Path, sql: str, params: tuple[Any, ...], *,
                 max_rows: int = MAX_ROWS, timeout_s: float = QUERY_TIMEOUT_S
                 ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """Run a query on a read-only connection with a row cap and a time limit."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    timer = threading.Timer(timeout_s, conn.interrupt)
    timer.start()
    try:
        conn.execute("PRAGMA query_only = ON")
        cursor = conn.execute(sql, params)
        columns = [d[0] for d in cursor.description or ()]
        rows = cursor.fetchmany(max_rows + 1)
        return columns, [tuple(r) for r in rows[:max_rows]], len(rows) > max_rows
    finally:
        timer.cancel()
        conn.close()


@dataclass
class _Reading:
    origin: str
    intent: Intent
    explanation: str = ""
    query: Any = None
    compiled: Any = None
    columns: list[str] = field(default_factory=list)
    preview: list[tuple[Any, ...]] = field(default_factory=list)
    error: str | None = None


def _realize(schema, db_path: str | Path, intent: Intent, origin: str) -> _Reading:
    reading = _Reading(origin, intent)
    try:
        reading.explanation = describe(schema, intent)
        reading.query = build_query(schema, intent)
        reading.compiled = compile_query(reading.query, schema)
        reading.columns, reading.preview, _ = execute_read(
            db_path, reading.compiled.sql, reading.compiled.params, max_rows=PREVIEW_ROWS)
    except (PlanError, CompileError, sqlite3.Error) as exc:
        reading.error = str(exc)
    return reading


def _evidence(reading: _Reading) -> dict[str, Any]:
    return {"row_count": len(reading.preview) if len(reading.preview) < PREVIEW_ROWS else f"{PREVIEW_ROWS}+",
            "first_rows": [[_short(v) for v in row] for row in reading.preview[:3]]}


def _short(value: Any) -> Any:
    return value[:60] if isinstance(value, str) else value


def _verify(question: str, readings: list[_Reading], ask, result: Answer) -> tuple[_Reading, float]:
    """Check the main reading against the request; compare with distinct alternatives.

    Returns the chosen reading and its verification probability.
    """
    main = readings[0]
    letters = [chr(ord("A") + i) for i in range(len(readings))]
    state: dict[str, Any] = {"request": question}
    questions: dict[str, Any] = {}
    if len(readings) == 1:
        state.update(interpretation=main.explanation, result=_evidence(main))
        questions["verify"] = noul(VERIFY_QUESTION)
    else:
        state["interpretations"] = {letter: {"meaning": r.explanation, "result": _evidence(r)}
                                    for letter, r in zip(letters, readings)}
        for letter in letters:
            questions[f"verify_{letter}"] = noul(
                {"question": f"Does interpretation {letter} in `interpretations` describe exactly what "
                             "`request` asks for, with nothing missing, nothing extra and the same "
                             "conditions?"})
        questions["pick"] = choice(
            "Which interpretation in `interpretations` answers `request` exactly?",
            {**{letter: f"interpretation {letter}" for letter in letters},
             "none": "none of them answers the request"})
    answers = ask(state, questions, "verification")
    if len(readings) == 1:
        p = answers["verify"].p or 0.0
        result.decisions.append(Decision("verify", "verification", main.explanation, None, "main", p))
        return main, p
    pick = answers["pick"]
    scores = {letter: answers[f"verify_{letter}"].p or 0.0 for letter in letters}
    result.decisions.append(Decision("pick", "verification", "which interpretation",
                                     {letter: r.explanation for letter, r in zip(letters, readings)},
                                     pick.choice, pick.top_probability, dict(pick.probabilities)))
    best = letters[0]
    candidate = pick.choice if pick.choice in letters else max(scores, key=lambda k: scores[k])
    if candidate != best and (pick.probabilities.get(candidate, 0) - pick.probabilities.get(best, 0)
                              >= SWITCH_MARGIN) and scores[candidate] >= scores[best]:
        best = candidate
    chosen = readings[letters.index(best)]
    return chosen, scores[best]


def _nothing(rows: list[tuple[Any, ...]], intent: Intent) -> bool:
    """No rows, or a single all-aggregate row of zeros/NULLs ('0 orders')."""
    if not rows:
        return True
    return (len(rows) == 1 and all(o.is_aggregate for o in intent.outputs)
            and all(v in (0, None) for v in rows[0]))


def _explain_empty(schema, db_path: str | Path, intent: Intent) -> str | None:
    """Why a query returned nothing: which condition removes every row, and for dates,
    the range the data actually covers.  Deterministic; nothing is changed or retried."""
    simple = [c for c in intent.conditions if c.kind in SIMPLE_CONDITIONS and c.column]
    blocking = []
    for cond in simple[:6]:
        relaxed = intent.with_(conditions=tuple(c for c in intent.conditions if c is not cond), limit=None,
                               offset=None, order=())
        try:
            compiled = compile_query(build_query(schema, relaxed), schema)
            _, rows, _ = execute_read(db_path, compiled.sql, compiled.params, max_rows=1)
        except (PlanError, CompileError, sqlite3.Error):
            continue
        if not _nothing(rows, relaxed):
            blocking.append(cond)
    if not blocking:
        return None
    parts = []
    for cond in blocking:
        text = condition_text(schema, cond, intent.subject).removeprefix("whose ").removeprefix("that ")
        column = schema.column(cond.column)
        span = ""
        if column.kind == "date" and column.formula is None:
            table = schema.table(column.table)
            try:
                _, rows, _ = execute_read(db_path, f'SELECT MIN("{column.name}"), MAX("{column.name}") '
                                                   f'FROM "{table.physical}"', (), max_rows=1)
                if rows and rows[0][0]:
                    span = f" (the data covers {rows[0][0]} to {rows[0][1]})"
            except sqlite3.Error:
                pass
        parts.append(f"{text}{span}")
    return "No rows match. Without this condition there would be results: " + "; ".join(parts) + "."


CALIBRATIONS: dict[str, tuple[tuple[float, float], ...]] = {"typesafe": CALIBRATION}


def calibrated(verification: float, alternatives: int,
               table: tuple[tuple[float, float], ...] = CALIBRATION) -> float:
    """Probability that an answer is right, from its verification score and the number of
    result-distinct alternative readings (calibrated on Spider train, see CALIBRATION)."""
    score = max(0.0, verification - ALTERNATIVE_PENALTY * alternatives)
    value = table[0][1]
    for bound, accuracy in table:
        if score >= bound:
            value = accuracy
    return value


def answer(db_path: str | Path, question: str, *, client: JevClient | None = None,
           strategy: Strategy | None = None, execute: bool = True,
           on_event: Callable[[dict[str, Any]], None] | None = None) -> Answer:
    started = time.monotonic()
    result = Answer(status=ERROR, question=question)
    timings = result.timings_ms

    def mark(phase: str, since: float) -> float:
        now = time.monotonic()
        timings[phase] = round((now - since) * 1000)
        if on_event:
            on_event({"kind": "phase", "phase": phase, "ms": timings[phase]})
        return now

    question = " ".join(question.split())
    if not question:
        result.status, result.message = REFUSED, "The request is empty."
        return result
    client = client or shared_client()
    t = time.monotonic()
    try:
        schema = load_schema(db_path)
    except (OSError, sqlite3.Error) as exc:
        result.message = f"Could not open the database: {exc}"
        return result
    grounding = ground(question, schema)
    t = mark("ground", t)

    def ask(state, questions, purpose):
        return client.ask(state, questions, usage=result.usage, log=result.exchanges,
                          purpose=purpose)

    try:
        interpretation, cands = interpret(grounding, ask, strategy)
    except JevError as exc:
        result.message = str(exc)
        return result
    t = mark("interpret", t)
    result.decisions = interpretation.decisions
    if interpretation.intent is None:
        result.status, result.message = REFUSED, interpretation.message
        return result

    # Build, compile and execute the main reading and its one-flip alternatives.
    readings: list[_Reading] = []
    seen_results: list[tuple] = []
    flips = [("main", interpretation.intent)]
    if strategy is None or strategy.verify:
        flips += alternatives(grounding, cands, interpretation, strategy)
    failure = None
    for origin, intent in flips:
        reading = _realize(grounding.schema, db_path, intent, origin)
        if reading.error:
            failure = failure or reading.error
            continue
        # Same rows with the columns in another order are the same answer, not an alternative.
        signature = (len(reading.columns),
                     tuple(sorted(tuple(sorted(map(repr, row))) for row in reading.preview)))
        if signature in seen_results:
            continue                      # same result as an earlier reading: not a real alternative
        seen_results.append(signature)
        readings.append(reading)
    t = mark("compile", t)
    if not readings:
        result.status = REFUSED
        result.message = f"No valid query could be built: {failure}"
        return result

    chosen = readings[0]
    if strategy is None or strategy.verify:
        try:
            chosen, confidence = _verify(question, readings, ask, result)
        except JevError as exc:
            result.message = str(exc)
            return result
        result.verification = confidence
        result.provider = getattr(getattr(client, "provider", None), "name", "") or result.usage.provider
        table = CALIBRATIONS.get(result.provider)
        result.confidence = calibrated(confidence, len(readings) - 1, table) if table else None
        t = mark("verify", t)

    result.intent = ir.to_dict(chosen.intent)
    result.explanation = chosen.explanation
    result.plan = ir.to_dict(chosen.query)
    result.sql, result.params, result.display_sql = (chosen.compiled.sql, chosen.compiled.params,
                                                     chosen.compiled.display_sql)
    result.alternatives = [{"explanation": r.explanation, "sql": r.compiled.sql,
                            "display_sql": r.compiled.display_sql, "params": list(r.compiled.params),
                            "columns": r.columns, "rows": [list(x) for x in r.preview],
                            "origin": r.origin}
                           for r in readings if r is not chosen]
    if execute:
        try:
            result.columns, result.rows, result.truncated = execute_read(
                db_path, chosen.compiled.sql, chosen.compiled.params)
        except sqlite3.Error as exc:
            result.status = ERROR
            result.message = f"SQLite rejected the query: {exc}"
            return result
        mark("execute", t)
    if execute and result.columns and _nothing(result.rows, chosen.intent):
        result.message = _explain_empty(grounding.schema, db_path, chosen.intent)
    uncertain = (result.confidence < LOW_CONFIDENCE if result.confidence is not None else
                 result.verification is not None and result.verification < UNCALIBRATED_UNCERTAIN)
    if uncertain:
        result.status = AMBIGUOUS
        result.message = ("This reading is uncertain. Check the interpretation"
                          + (" or pick one of the alternatives below." if result.alternatives else "."))
        timings["total"] = round((time.monotonic() - started) * 1000)
        return result
    result.status = ANSWERED
    timings["total"] = round((time.monotonic() - started) * 1000)
    return result
