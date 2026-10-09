"""Typed relational IR: the only thing the SQL compiler accepts.

The interpreter builds these immutable nodes from grounded decisions; the
compiler turns them into parameterized SQLite SQL; ``validate`` rejects plans
that reference unknown identifiers or break SQL structure rules.  Nodes are
plain dataclasses so a plan can be inspected, serialized and diffed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Union

AGGREGATES = ("COUNT", "SUM", "AVG", "MIN", "MAX")
COMPARISONS = ("=", "!=", "<", "<=", ">", ">=")
ARITHMETIC = ("+", "-", "*", "/")
SET_OPERATORS = ("UNION", "UNION ALL", "INTERSECT", "EXCEPT")
# SQLite scalar functions the compiler accepts, with (min, max) argument counts.
FUNCTIONS = {
    "LOWER": (1, 1), "UPPER": (1, 1), "LENGTH": (1, 1), "TRIM": (1, 2), "ABS": (1, 1),
    "ROUND": (1, 2), "SUBSTR": (2, 3), "COALESCE": (2, 8), "IFNULL": (2, 2),
    "STRFTIME": (2, 4), "DATE": (1, 4), "JULIANDAY": (1, 4), "CAST_REAL": (1, 1),
    "CAST_INTEGER": (1, 1), "INSTR": (2, 2), "REPLACE": (3, 3), "NULLIF": (2, 2),
}


# ---- expressions ------------------------------------------------------------------

@dataclass(frozen=True)
class Col:
    """A column of a table instance; ``source`` is the TableRef alias in scope."""
    source: str
    column: str


@dataclass(frozen=True)
class Star:
    source: str | None = None


@dataclass(frozen=True)
class Lit:
    value: Any


@dataclass(frozen=True)
class Agg:
    func: str
    arg: "Expr"
    distinct: bool = False


@dataclass(frozen=True)
class BinOp:
    op: str
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Func:
    name: str
    args: tuple["Expr", ...]


@dataclass(frozen=True)
class Case:
    whens: tuple[tuple["Cond", "Expr"], ...]
    otherwise: "Expr | None" = None


@dataclass(frozen=True)
class ScalarQuery:
    query: "Query"


Expr = Union[Col, Star, Lit, Agg, BinOp, Func, Case, ScalarQuery]


# ---- conditions -------------------------------------------------------------------

@dataclass(frozen=True)
class Compare:
    left: Expr
    op: str
    right: Expr


@dataclass(frozen=True)
class Between:
    expr: Expr
    low: Expr
    high: Expr
    negated: bool = False


@dataclass(frozen=True)
class InList:
    expr: Expr
    values: tuple[Expr, ...]
    negated: bool = False


@dataclass(frozen=True)
class InQuery:
    expr: Expr
    query: "Query"
    negated: bool = False


@dataclass(frozen=True)
class Exists:
    query: "Query"
    negated: bool = False


@dataclass(frozen=True)
class Like:
    expr: Expr
    pattern: str
    negated: bool = False


@dataclass(frozen=True)
class IsNull:
    expr: Expr
    negated: bool = False


@dataclass(frozen=True)
class And:
    items: tuple["Cond", ...]


@dataclass(frozen=True)
class Or:
    items: tuple["Cond", ...]


@dataclass(frozen=True)
class Not:
    item: "Cond"


Cond = Union[Compare, Between, InList, InQuery, Exists, Like, IsNull, And, Or, Not]


# ---- queries ----------------------------------------------------------------------

@dataclass(frozen=True)
class TableRef:
    table: str
    alias: str


@dataclass(frozen=True)
class Join:
    right: TableRef
    on: tuple[tuple[Col, Col], ...]
    kind: str = "INNER"          # INNER | LEFT


@dataclass(frozen=True)
class From:
    base: TableRef
    joins: tuple[Join, ...] = ()

    @property
    def refs(self) -> tuple[TableRef, ...]:
        return (self.base,) + tuple(j.right for j in self.joins)


@dataclass(frozen=True)
class SelectItem:
    expr: Expr
    alias: str | None = None


@dataclass(frozen=True)
class OrderItem:
    expr: Expr
    desc: bool = False


@dataclass(frozen=True)
class Select:
    items: tuple[SelectItem, ...]
    from_: From
    where: Cond | None = None
    group_by: tuple[Expr, ...] = ()
    having: Cond | None = None
    order_by: tuple[OrderItem, ...] = ()
    limit: int | None = None
    offset: int | None = None
    distinct: bool = False


@dataclass(frozen=True)
class SetOp:
    op: str
    left: "Query"
    right: "Query"
    order_by: tuple[OrderItem, ...] = ()   # rejected by the compiler (not yet supported)
    limit: int | None = None


Query = Union[Select, SetOp]


# ---- generic helpers --------------------------------------------------------------

def to_dict(node: Any) -> Any:
    """Serialize a plan for debugging/UI: dataclasses become tagged dicts."""
    if is_dataclass(node) and not isinstance(node, type):
        out: dict[str, Any] = {"node": type(node).__name__}
        for f in fields(node):
            value = getattr(node, f.name)
            if value in (None, (), False) and f.name not in ("value",):
                continue
            out[f.name.rstrip("_")] = to_dict(value)
        return out
    if isinstance(node, (tuple, list)):
        return [to_dict(v) for v in node]
    return node


def walk(node: Any):
    """Yield every IR node (pre-order), descending into subqueries."""
    if is_dataclass(node) and not isinstance(node, type):
        yield node
        for f in fields(node):
            yield from walk(getattr(node, f.name))
    elif isinstance(node, (tuple, list)):
        for item in node:
            yield from walk(item)


def conjuncts(cond: Cond | None) -> list[Cond]:
    if cond is None:
        return []
    if isinstance(cond, And):
        return [c for item in cond.items for c in conjuncts(item)]
    return [cond]


def conjoin(items: list[Cond]) -> Cond | None:
    flat = [c for item in items for c in conjuncts(item)]
    if not flat:
        return None
    return flat[0] if len(flat) == 1 else And(tuple(flat))


def has_aggregate(expr: Any) -> bool:
    """True when ``expr`` aggregates at this query level (subqueries excluded)."""
    if isinstance(expr, Agg):
        return True
    if isinstance(expr, (ScalarQuery, InQuery, Exists, Select, SetOp)):
        return False
    if is_dataclass(expr) and not isinstance(expr, type):
        return any(has_aggregate(getattr(expr, f.name)) for f in fields(expr))
    if isinstance(expr, (tuple, list)):
        return any(has_aggregate(item) for item in expr)
    return False


@dataclass
class ValidationIssue:
    code: str
    message: str
    node: Any = field(default=None, repr=False)
