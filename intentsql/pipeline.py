"""End-to-end request handling: ground -> interpret -> build -> compile -> execute.

``answer`` is the single entry point used by the CLI, the web app and the
benchmarks.  It always returns an ``Answer``; failures are reported as a
status with a reason, never as a partially executed query.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from intentsql import ir
from intentsql.compiler import CompileError, compile_query
from intentsql.grounding import ground
from intentsql.intent import PlanError, build_query, describe
from intentsql.interpret import Decision, Strategy, interpret
from intentsql.jev import Exchange, JevClient, JevError, Usage, shared_client
from intentsql.schema import load_schema

ANSWERED, AMBIGUOUS, REFUSED, ERROR = "answered", "ambiguous", "refused", "error"
MAX_ROWS = 1000
QUERY_TIMEOUT_S = 15.0


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
        interpretation, _ = interpret(grounding, ask, strategy)
    except JevError as exc:
        result.message = str(exc)
        return result
    t = mark("interpret", t)
    result.decisions = interpretation.decisions
    intent = interpretation.intent
    if intent is None:
        result.status, result.message = REFUSED, interpretation.message
        return result
    result.intent = ir.to_dict(intent)
    result.explanation = describe(schema, intent)
    try:
        query = build_query(schema, intent)
        compiled = compile_query(query, schema)
    except (PlanError, CompileError) as exc:
        result.status = REFUSED
        result.message = f"No valid query could be built: {exc}"
        return result
    result.plan = ir.to_dict(query)
    result.sql, result.params, result.display_sql = compiled.sql, compiled.params, compiled.display_sql
    t = mark("compile", t)
    if execute:
        try:
            result.columns, result.rows, result.truncated = execute_read(
                db_path, compiled.sql, compiled.params)
        except sqlite3.Error as exc:
            result.status = ERROR
            result.message = f"SQLite rejected the query: {exc}"
            return result
        mark("execute", t)
    result.status = ANSWERED
    timings["total"] = round((time.monotonic() - started) * 1000)
    return result
