"""Execution-result equivalence in the style of the Spider test-suite evaluator.

Two results match when they contain the same rows (as a multiset, or as an
ordered list when the reference query orders its output), allowing the
predicted columns to appear in a different order.  Floats are compared after
rounding so that ``AVG(x)`` and ``SUM(x) / COUNT(x)`` style differences in the
last bits do not count as semantic failures.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import permutations, product
from pathlib import Path
from typing import Any

Row = tuple[Any, ...]

_FLOAT_DIGITS = 6
MAX_ROWS = 100_000


def _norm(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if value.is_integer():
            return int(value)
        return round(value, _FLOAT_DIGITS)
    if isinstance(value, bytes):
        return value.hex()
    return value


def normalize_rows(rows: Iterable[Sequence[Any]]) -> list[Row]:
    return [tuple(_norm(value) for value in row) for row in rows]


def _column_permutations(reference: list[Row], predicted: list[Row]) -> Iterable[tuple[int, ...]]:
    """Yield predicted-column orders consistent with the reference column values."""
    width = len(reference[0])
    if width == 1:
        yield (0,)
        return
    reference_sets = [{row[i] for row in reference} for i in range(width)]
    if width > 6:
        # Restrict candidates per column using a sample of predicted rows.
        sample = predicted[:20]
        candidates = []
        for column in range(width):
            options = [i for i in range(width)
                       if all(row[i] in reference_sets[column] for row in sample)]
            candidates.append(options or list(range(width)))
        for perm in product(*candidates):
            if len(set(perm)) == width:
                yield perm
        return
    yield from permutations(range(width))


def results_equivalent(reference: list[Row], predicted: list[Row], *, order_matters: bool,
                       allow_column_permutation: bool = True) -> bool:
    if not reference and not predicted:
        return True
    if len(reference) != len(predicted):
        return False
    if len(reference[0]) != len(predicted[0]):
        return False
    perms = (_column_permutations(reference, predicted) if allow_column_permutation
             else [tuple(range(len(reference[0])))])
    reference_counter = Counter(reference)
    for perm in perms:
        permuted = [tuple(row[i] for i in perm) for row in predicted]
        if order_matters:
            if permuted == reference:
                return True
        elif Counter(permuted) == reference_counter:
            return True
    return False


def set_equivalent(reference: list[Row], predicted: list[Row]) -> bool:
    """Lenient comparison: same distinct rows, any order, any column order."""
    ref = sorted(set(reference), key=repr)
    pred = sorted(set(predicted), key=repr)
    if not ref and not pred:
        return True
    if not ref or not pred or len(ref[0]) != len(pred[0]):
        return False
    return results_equivalent(ref, pred, order_matters=False)


@dataclass(frozen=True)
class Execution:
    rows: list[Row] | None
    error: str | None = None
    truncated: bool = False


def execute(db_path: str | Path, sql: str, params: Sequence[Any] = (), *,
            timeout_s: float = 20.0, max_rows: int = MAX_ROWS) -> Execution:
    """Execute a read query read-only with a wall-clock guard."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    timer = threading.Timer(timeout_s, conn.interrupt)
    timer.start()
    try:
        cursor = conn.execute(sql, tuple(params))
        rows = cursor.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        return Execution(normalize_rows(rows[:max_rows]), None, truncated)
    except sqlite3.Error as exc:
        return Execution(None, f"{type(exc).__name__}: {exc}")
    finally:
        timer.cancel()
        conn.close()


def order_matters(reference_sql: str) -> bool:
    return "order by" in " ".join(reference_sql.lower().split())
