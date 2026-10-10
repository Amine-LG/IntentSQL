"""Structural diagnosis of a wrong prediction against its reference SQL.

Used only for error taxonomy in benchmark reports.  Execution equivalence is
the correctness criterion; this module explains *why* a query differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:  # Benchmark-only dependency.
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - optional
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Shape:
    tables: frozenset[str]
    select_columns: frozenset[str]
    aggregates: frozenset[str]
    filter_columns: frozenset[str]
    group_columns: frozenset[str]
    having: bool
    order_columns: frozenset[str]
    order_desc: tuple[bool, ...]
    limit: str | None
    nested: bool
    set_op: str | None
    distinct: bool
    join_count: int


def _name(node: Any) -> str:
    return node.name.lower() if node is not None and hasattr(node, "name") else ""


def _columns(node: Any) -> frozenset[str]:
    if node is None:
        return frozenset()
    return frozenset(_name(c) for c in node.find_all(exp.Column))


def shape(sql: str) -> Shape | None:
    if sqlglot is None:
        return None
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return None
    set_op = None
    for kind, label in ((exp.Intersect, "intersect"), (exp.Union, "union"), (exp.Except, "except")):
        if isinstance(tree, kind):
            set_op = label
    root = tree
    while isinstance(root, (exp.Union, exp.Intersect, exp.Except)):
        root = root.this
    if not isinstance(root, exp.Select):
        return None
    tables = frozenset(_name(t) for t in tree.find_all(exp.Table))
    selects = root.expressions
    aggregates = frozenset(
        f"{type(a).__name__.lower()}({','.join(sorted(_columns(a))) or '*'})"
        for s in selects for a in s.find_all(exp.AggFunc))
    where = root.args.get("where")
    group = root.args.get("group")
    order = root.args.get("order")
    limit = root.args.get("limit")
    nested = any(isinstance(s, exp.Select) and s is not root for s in root.find_all(exp.Select))
    joins = root.args.get("joins") or []
    return Shape(
        tables=tables,
        select_columns=frozenset().union(*(_columns(s) for s in selects)) if selects else frozenset(),
        aggregates=aggregates,
        filter_columns=_columns(where),
        group_columns=_columns(group),
        having=root.args.get("having") is not None,
        order_columns=_columns(order),
        order_desc=tuple(bool(o.args.get("desc")) for o in (order.expressions if order else [])),
        limit=limit.sql() if limit is not None else None,
        nested=nested,
        set_op=set_op,
        distinct=root.args.get("distinct") is not None,
        join_count=len(joins),
    )


def diagnose(reference_sql: str, predicted_sql: str | None) -> list[str]:
    """Return error categories explaining a mismatch (may be empty if unknown)."""
    if not predicted_sql:
        return ["no_sql"]
    ref, pred = shape(reference_sql), shape(predicted_sql)
    if ref is None or pred is None:
        return ["unparsed"]
    errors = []
    if ref.set_op != pred.set_op:
        errors.append("set_operation")
    if ref.nested != pred.nested and not (ref.set_op or pred.set_op):
        errors.append("nesting")
    if ref.tables != pred.tables:
        errors.append("wrong_table" if not (ref.tables & pred.tables) else "join")
    if ref.select_columns != pred.select_columns:
        errors.append("wrong_column")
    if ref.aggregates != pred.aggregates:
        errors.append("aggregation")
    if ref.filter_columns != pred.filter_columns:
        errors.append("filter")
    if ref.group_columns != pred.group_columns or ref.having != pred.having:
        errors.append("grouping")
    if ref.order_columns != pred.order_columns or ref.order_desc != pred.order_desc:
        errors.append("ordering")
    if (ref.limit is None) != (pred.limit is None):
        errors.append("limit")
    if ref.distinct != pred.distinct:
        errors.append("distinct")
    return errors or ["value_or_detail"]
