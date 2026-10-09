"""Semantic query plan ("intent") and its deterministic translation to IR.

An ``Intent`` is what the user asked for, expressed with grounded schema
objects but without SQL mechanics: which values to return, which conditions
hold, how results are grouped, ranked and limited.  ``build_query`` derives
the SQL structure (join tree, subqueries, set operations, GROUP BY) from it.
``describe`` renders it as plain English for the UI and for Jev comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from intentsql import ir
from intentsql.schema import Schema

TEXT_OPS = {"contains": "%{}%", "starts": "{}%", "ends": "%{}"}
OP_WORDS = {"=": "is", "!=": "is not", ">": "is greater than", "<": "is less than",
            ">=": "is at least", "<=": "is at most", "contains": "contains",
            "starts": "starts with", "ends": "ends with", "not_contains": "does not contain"}
FUNC_WORDS = {"COUNT": "number of", "SUM": "total", "AVG": "average", "MIN": "minimum",
              "MAX": "maximum"}


class PlanError(ValueError):
    """The intent cannot be realized over this schema (e.g. disconnected tables)."""


@dataclass(frozen=True)
class OutputItem:
    func: str | None = None          # None | COUNT | SUM | AVG | MIN | MAX
    column: str | None = None        # "table.column"; None means COUNT(*) of ``table``
    table: str | None = None
    distinct: bool = False           # COUNT(DISTINCT column)

    @property
    def is_aggregate(self) -> bool:
        return self.func is not None

    @property
    def tables(self) -> tuple[str, ...]:
        if self.column:
            return (self.column.split(".", 1)[0],)
        return (self.table,) if self.table else ()


@dataclass(frozen=True)
class Condition:
    """One restriction.  ``kind`` selects the meaning of the remaining fields.

    compare      column op values[0]           (op in = != < <= > >=)
    text         column op values[0]           (op in contains starts ends not_contains)
    between      column between values[0..1]
    in_values    column in values (negated: not in)
    null         column is null (negated: is not null)
    absent       subject has no related ``table`` rows (matching ``inner``)
    related      subject has related ``table`` rows (realized as an inner join)
    same_as      column equals that of the record(s) matching ``inner`` ("same department as Clara")
    vs_aggregate column op func(agg_column) over all rows of its table
    having       func(column or COUNT(*) of table) op values[0], per group
    """
    kind: str
    column: str | None = None
    op: str = "="
    values: tuple[Any, ...] = ()
    negated: bool = False
    func: str | None = None
    table: str | None = None
    inner: tuple["Condition", ...] = ()

    @property
    def tables(self) -> tuple[str, ...]:
        if self.kind == "absent":
            return ()
        if self.kind == "related":
            return (self.table,) if self.table else ()
        if self.column:
            return (self.column.split(".", 1)[0],)
        return (self.table,) if self.kind == "having" and self.table else ()


@dataclass(frozen=True)
class Ordering:
    func: str | None = None
    column: str | None = None
    table: str | None = None          # COUNT(*) of table when column is None
    desc: bool = False

    @property
    def is_aggregate(self) -> bool:
        return self.func is not None


@dataclass(frozen=True)
class Intent:
    outputs: tuple[OutputItem, ...]
    conditions: tuple[Condition, ...] = ()
    connector: str = "AND"            # how WHERE conditions combine
    group_by: tuple[str, ...] = ()    # explicit group keys (column keys)
    order: tuple[Ordering, ...] = ()
    limit: int | None = None
    offset: int | None = None
    distinct: bool = False
    both: tuple[int, int] | None = None   # indexes of two conditions that must hold via different related rows
    excluded: tuple[Condition, ...] = ()  # results matching these are removed (EXCEPT)
    subject: str | None = None
    join_choices: tuple[tuple[str, str], ...] = ()   # (table pair key, fk key) disambiguations

    def with_(self, **changes: Any) -> "Intent":
        return replace(self, **changes)


# ---- English rendering ------------------------------------------------------------

def column_phrase(schema: Schema, key: str) -> str:
    column = schema.column(key)
    table = schema.table(column.table)
    words = column.label
    t_words = set(table.label.split())
    if set(words.split()) & t_words or column.label in ("name", "id"):
        return f"{table.label} {words}" if not words.startswith(table.label) else words
    return f"{table.label} {words}"


def describe_output(schema: Schema, item: OutputItem) -> str:
    if item.column is None:
        return f"the number of {schema.table(item.table).label} records"
    phrase = column_phrase(schema, item.column)
    if item.func is None:
        return f"the {phrase}"
    if item.func == "COUNT":
        return f"the number of {'distinct ' if item.distinct else ''}{phrase} values"
    return f"the {FUNC_WORDS[item.func]} {phrase}"


def _value(v: Any) -> str:
    return f"'{v}'" if isinstance(v, str) else str(v)


def describe_condition(schema: Schema, cond: Condition, subject: str | None = None) -> str:
    if cond.kind == "related":
        return f"has related {schema.table(cond.table).label} records"
    if cond.kind == "same_as":
        inner = " and ".join(describe_condition(schema, c) for c in cond.inner)
        return f"{column_phrase(schema, cond.column)} is the same as for the records where {inner}"
    if cond.kind == "absent":
        inner = " and ".join(describe_condition(schema, c) for c in cond.inner)
        what = schema.table(cond.table).label
        base = f"has no related {what} records"
        return base + (f" where {inner}" if inner else "")
    if cond.kind == "having":
        target = (f"the number of {schema.table(cond.table).label} records" if not cond.column
                  else f"the {FUNC_WORDS[cond.func or 'COUNT']} {column_phrase(schema, cond.column)}")
        return f"{target} per group {OP_WORDS[cond.op]} {_value(cond.values[0])}"
    phrase = column_phrase(schema, cond.column) if cond.column else "?"
    if cond.kind in ("compare", "text"):
        return f"{phrase} {OP_WORDS[cond.op]} {_value(cond.values[0])}"
    if cond.kind == "between":
        return f"{phrase} is between {_value(cond.values[0])} and {_value(cond.values[1])}"
    if cond.kind == "in_values":
        values = ", ".join(_value(v) for v in cond.values)
        return f"{phrase} is {'not ' if cond.negated else ''}one of {values}"
    if cond.kind == "null":
        return f"{phrase} is {'present' if cond.negated else 'missing (null)'}"
    if cond.kind == "vs_aggregate":
        return (f"{phrase} {OP_WORDS[cond.op]} the {FUNC_WORDS[cond.func or 'AVG']} "
                f"{column_phrase(schema, cond.column)} of all {schema.table(cond.column.split('.')[0]).label} records")
    return f"{cond.kind} {phrase}"


def describe(schema: Schema, intent: Intent) -> str:
    outputs = ", ".join(describe_output(schema, o) for o in intent.outputs)
    text = f"Return {'distinct values of ' if intent.distinct else ''}{outputs}"
    group = _effective_groups(intent)
    if group:
        text += " for each " + " and ".join(column_phrase(schema, g) for g in group)
    where = [c for c in intent.conditions if c.kind != "having"]
    if intent.excluded:
        text += ", excluding those where " + " and ".join(
            describe_condition(schema, c) for c in intent.excluded)
    if where:
        joiner = " and " if intent.connector == "AND" else " or "
        text += " where " + joiner.join(describe_condition(schema, c) for c in where)
    having = [c for c in intent.conditions if c.kind == "having"]
    if having:
        text += ", keeping only groups where " + " and ".join(
            describe_condition(schema, c) for c in having)
    if intent.order:
        parts = []
        for o in intent.order:
            target = (describe_output(schema, OutputItem(o.func, o.column, o.table))
                      if o.is_aggregate else f"the {column_phrase(schema, o.column)}")
            parts.append(f"{target} {'from highest to lowest' if o.desc else 'from lowest to highest'}")
        text += ", sorted by " + ", then ".join(parts)
    if intent.limit is not None:
        text += (", only the first result" if intent.limit == 1
                 else f", only the first {intent.limit} results")
    if intent.offset:
        text += f" after skipping {intent.offset}"
    return text + "."


# ---- building IR ------------------------------------------------------------------

def _effective_groups(intent: Intent) -> tuple[str, ...]:
    if intent.group_by:
        return intent.group_by
    has_agg = (any(o.is_aggregate for o in intent.outputs)
               or any(o.is_aggregate for o in intent.order)
               or any(c.kind == "having" for c in intent.conditions))
    plain = tuple(o.column for o in intent.outputs if not o.is_aggregate and o.column)
    if has_agg and plain:
        return tuple(dict.fromkeys(plain))
    return ()


@dataclass
class _Scope:
    schema: Schema
    aliases: dict[str, str] = field(default_factory=dict)   # table -> alias
    prefix: str = ""

    def alias(self, table: str) -> str:
        return self.aliases[table]

    def col(self, key: str) -> ir.Col:
        table, _, column = key.partition(".")
        return ir.Col(self.alias(self.schema.table(table).name), self.schema.column(key).name)


def _from(schema: Schema, tables: list[str], root: str, prefix: str,
          join_choices: dict[str, str]) -> tuple[ir.From, _Scope]:
    steps = schema.join_tree(tables, root)
    if steps is None:
        raise PlanError("The requested tables are not connected by any relationship: "
                        + ", ".join(sorted(set(tables))))
    scope = _Scope(schema, {root: f"{prefix}{root}"}, prefix)
    joins = []
    for step in steps:
        fk = step.fk
        pair = "|".join(sorted((step.from_table, step.table)))
        if pair in join_choices:
            options = {f.key: f for f in schema.parallel_edges(step.from_table, step.table)}
            fk = options.get(join_choices[pair], fk)
        scope.aliases[step.table] = f"{prefix}{step.table}"
        pairs = fk.pairs_from(step.from_table)
        on = tuple((ir.Col(scope.alias(step.from_table), a), ir.Col(scope.alias(step.table), b))
                   for a, b in pairs)
        joins.append(ir.Join(ir.TableRef(step.table, scope.alias(step.table)), on))
    return ir.From(ir.TableRef(root, scope.alias(root)), tuple(joins)), scope


def _agg_expr(scope: _Scope, func: str | None, column: str | None, table: str | None,
              distinct: bool = False) -> ir.Expr:
    if column is None:
        return ir.Agg("COUNT", ir.Star())
    expr = scope.col(column)
    return ir.Agg(func, expr, distinct) if func else expr


def _condition(schema: Schema, scope: _Scope, cond: Condition, counter: list[int],
               join_choices: dict[str, str]) -> ir.Cond:
    if cond.kind == "compare":
        return ir.Compare(scope.col(cond.column), cond.op, ir.Lit(cond.values[0]))
    if cond.kind == "text":
        if cond.op == "not_contains":
            return ir.Like(scope.col(cond.column), f"%{cond.values[0]}%", negated=True)
        return ir.Like(scope.col(cond.column), TEXT_OPS[cond.op].format(cond.values[0]))
    if cond.kind == "between":
        return ir.Between(scope.col(cond.column), ir.Lit(cond.values[0]), ir.Lit(cond.values[1]))
    if cond.kind == "in_values":
        return ir.InList(scope.col(cond.column), tuple(ir.Lit(v) for v in cond.values), cond.negated)
    if cond.kind == "null":
        return ir.IsNull(scope.col(cond.column), cond.negated)
    if cond.kind == "having":
        expr = _agg_expr(scope, cond.func, cond.column, cond.table)
        return ir.Compare(expr, cond.op, ir.Lit(cond.values[0]))
    if cond.kind == "vs_aggregate":
        counter[0] += 1
        table = cond.column.split(".", 1)[0]
        prefix = f"q{counter[0]}_"
        inner_from, inner_scope = _from(schema, [table], table, prefix, join_choices)
        sub = ir.Select(items=(ir.SelectItem(ir.Agg(cond.func or "AVG", inner_scope.col(cond.column))),),
                        from_=inner_from)
        return ir.Compare(scope.col(cond.column), cond.op, ir.ScalarQuery(sub))
    if cond.kind == "absent":
        return _absent(schema, scope, cond, counter, join_choices)
    if cond.kind == "same_as":
        counter[0] += 1
        table = cond.column.split(".", 1)[0]
        tables = [table] + [c.column.split(".", 1)[0] for c in cond.inner if c.column]
        inner_from, inner_scope = _from(schema, tables, table, f"q{counter[0]}_", join_choices)
        where = ir.conjoin([_condition(schema, inner_scope, c, counter, join_choices)
                            for c in cond.inner])
        sub = ir.Select(items=(ir.SelectItem(inner_scope.col(cond.column)),), from_=inner_from,
                        where=where)
        return ir.InQuery(scope.col(cond.column), sub)
    raise PlanError(f"Unsupported condition kind {cond.kind}")


def _absent(schema: Schema, scope: _Scope, cond: Condition, counter: list[int],
            join_choices: dict[str, str]) -> ir.Cond:
    subject = next(iter(scope.aliases))
    target = cond.table
    paths = schema.shortest_paths(subject, target, limit=1)
    if not paths or not paths[0]:
        raise PlanError(f"{subject} is not related to {target}")
    path = paths[0]
    first = path[0]
    # Subquery over the first hop table (and onwards), returning its link column.
    counter[0] += 1
    prefix = f"q{counter[0]}_"
    tables = [first.table] + [s.table for s in path[1:]]
    tables += [c.column.split(".", 1)[0] for c in cond.inner if c.column]
    inner_from, inner_scope = _from(schema, tables, first.table, prefix, join_choices)
    pairs = first.pairs
    subject_col, link_col = pairs[0]
    where = [ir.IsNull(ir.Col(inner_scope.alias(first.table), link_col), negated=True)]
    where += [_condition(schema, inner_scope, c, counter, join_choices) for c in cond.inner]
    sub = ir.Select(items=(ir.SelectItem(ir.Col(inner_scope.alias(first.table), link_col)),),
                    from_=inner_from, where=ir.conjoin(where))
    return ir.InQuery(ir.Col(scope.alias(subject), subject_col), sub, negated=True)


def query_tables(intent: Intent) -> list[str]:
    tables: list[str] = []
    for item in intent.outputs:
        tables.extend(item.tables)
    for cond in intent.conditions:
        tables.extend(cond.tables)
    for key in intent.group_by:
        tables.append(key.split(".", 1)[0])
    for order in intent.order:
        if order.column:
            tables.append(order.column.split(".", 1)[0])
        elif order.table:
            tables.append(order.table)
    return list(dict.fromkeys(tables))


def build_query(schema: Schema, intent: Intent) -> ir.Query:
    if not intent.outputs:
        raise PlanError("Nothing to return")
    join_choices = dict(intent.join_choices)
    if intent.excluded:
        kept = intent.with_(excluded=())
        removed = intent.with_(excluded=(), conditions=intent.excluded, both=None, order=(),
                               limit=None, offset=None, distinct=False, group_by=())
        if kept.order or kept.limit is not None:
            raise PlanError("Ordering combined with an exclusion is not supported")
        return ir.SetOp("EXCEPT", build_query(schema, kept) if kept.both is None
                        else _select(schema, kept.with_(both=None), "", join_choices),
                        _select(schema, removed, "x_", join_choices))
    if intent.both is not None:
        first, second = intent.both
        shared = [c for i, c in enumerate(intent.conditions) if i not in (first, second)]
        left = intent.with_(conditions=tuple(shared + [intent.conditions[first]]), both=None,
                            order=(), limit=None, distinct=False)
        right = intent.with_(conditions=tuple(shared + [intent.conditions[second]]), both=None,
                             order=(), limit=None, distinct=False)
        return ir.SetOp("INTERSECT", _select(schema, left, "a_", join_choices),
                        _select(schema, right, "b_", join_choices))
    return _select(schema, intent, "", join_choices)


def _select(schema: Schema, intent: Intent, prefix: str, join_choices: dict[str, str]) -> ir.Select:
    tables = query_tables(intent)
    root = intent.subject if intent.subject in tables else (tables[0] if tables else intent.subject)
    if root is None:
        raise PlanError("No source table")
    if intent.subject and intent.subject not in tables:
        tables = [intent.subject] + tables
        root = intent.subject
    from_, scope = _from(schema, tables, root, prefix, join_choices)
    counter = [0]
    items = tuple(ir.SelectItem(_agg_expr(scope, o.func, o.column, o.table, o.distinct))
                  for o in intent.outputs)
    where_conds = [_condition(schema, scope, c, counter, join_choices)
                   for c in intent.conditions if c.kind not in ("having", "related")]
    if intent.connector == "OR" and len(where_conds) > 1:
        where: ir.Cond | None = ir.Or(tuple(where_conds))
    else:
        where = ir.conjoin(where_conds)
    having_conds = [_condition(schema, scope, c, counter, join_choices)
                    for c in intent.conditions if c.kind == "having"]
    groups = tuple(scope.col(key) for key in _effective_groups(intent))
    order = tuple(ir.OrderItem(_agg_expr(scope, o.func, o.column, o.table), o.desc)
                  for o in intent.order)
    return ir.Select(items=items, from_=from_, where=where, group_by=groups,
                     having=ir.conjoin(having_conds), order_by=order, limit=intent.limit,
                     offset=intent.offset, distinct=intent.distinct)
