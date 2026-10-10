"""Semantic query plan ("intent") and its deterministic translation to IR.

An ``Intent`` is what the user asked for, expressed with grounded schema
objects but without SQL mechanics: which values to return, which conditions
hold, how results are grouped, ranked and limited.  ``build_query`` derives
the SQL structure (join tree, subqueries, set operations, GROUP BY) from it.
``render.describe`` renders it as plain English for the UI and for Jev comparisons.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Any

from intentsql import ir
from intentsql.schema import DATE, Schema
from intentsql.text import lemma

TEXT_OPS = {"contains": "%{}%", "starts": "{}%", "ends": "%{}"}
# Filters through one-to-many relationships are membership tests (no repeated rows).
SEMI_JOIN_FILTERS = os.environ.get("INTENTSQL_SEMI_JOIN", "1") != "0"
ELAPSED_UNITS = {"years": 365.25, "months": 30.4375, "days": 1.0}
DATE_PARTS = {"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d", "weekday": "%w", "hour": "%H"}
FUNC_WORDS = {"COUNT": "number of", "SUM": "total", "AVG": "average", "MIN": "minimum",
              "MAX": "maximum", "PCT": "percentage of"}


class PlanError(ValueError):
    """The intent cannot be realized over this schema (e.g. disconnected tables)."""


@dataclass(frozen=True)
class OutputItem:
    func: str | None = None          # None | COUNT | SUM | AVG | MIN | MAX | PCT
    column: str | None = None        # "table.column"; None means COUNT(*) of ``table``
    table: str | None = None
    distinct: bool = False           # COUNT(DISTINCT column)
    star: bool = False               # every column of ``table`` ("all information about ...")
    per: str | None = None           # nested: func over ``per`` records of each one's SUM(column)/COUNT
    where: tuple[Condition, ...] = ()   # PCT: the share of counted records matching these

    @property
    def is_aggregate(self) -> bool:
        return self.func is not None

    @property
    def tables(self) -> tuple[str, ...]:
        if self.per:
            return (self.per,)
        if self.column:
            return (self.column.split(".", 1)[0],)
        return (self.table,) if self.table else ()


@dataclass(frozen=True)
class Condition:
    """One restriction.  ``kind`` selects the meaning of the remaining fields.

    compare      column op values[0]           (op in = != < <= > >=)
    text         column op values[0]           (op in contains starts ends not_contains)
    between      column between values[0..1]
    period       values[0] <= column < values[1] (dates; values[2] names the period)
    in_values    column in values (negated: not in)
    null         column is null (negated: is not null)
    absent       subject has no related ``table`` rows (matching ``inner``)
    related      subject has related ``table`` rows (semi-join: IN, no row multiplication)
    same_as      column equals that of the record(s) matching ``inner`` ("same department as Clara")
    having_vs    func(column) per group op the overall func(column) of its table
    top_group    column op the most (func=DESC) / least (ASC) common value of that column
    vs_group     column op func(column) over the records sharing ``table`` (a column key):
                 'earning more than the average salary of their department' (correlated)
    vs_aggregate column op func(column) over the rows of its table (those matching ``inner``)
    having       func(column or COUNT(*) of table) op values[0], per group
    columns      column op values[0], where values[0] is another column key ('support < oppose')
    all / any    every / at least one of ``inner`` (one record: first and last name of a person)
    """
    kind: str
    column: str | None = None
    op: str = "="
    values: tuple[Any, ...] = ()
    negated: bool = False
    func: str | None = None
    table: str | None = None
    inner: tuple[Condition, ...] = ()

    @property
    def col(self) -> str:
        """The column of a kind that always has one (compare, between, vs_* ...)."""
        if self.column is None:
            raise PlanError(f"A {self.kind} condition needs a column")
        return self.column

    @property
    def tables(self) -> tuple[str, ...]:
        if self.kind in ("all", "any"):
            return tuple(dict.fromkeys(t for c in self.inner for t in c.tables))
        if self.kind == "absent":
            return ()
        if self.kind == "related":
            return ()      # realized as a semi-join (IN), so it never multiplies rows
        if self.kind == "columns" and self.column:
            return tuple(dict.fromkeys((self.column.split(".", 1)[0], self.values[0].split(".", 1)[0])))
        if self.column:
            return (self.column.split(".", 1)[0],)
        return (self.table,) if self.kind in ("having", "having_vs") and self.table else ()


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
    via: tuple[str, ...] = ()             # tables a join path must pass through (chosen route)
    subject: str | None = None
    join_choices: tuple[tuple[str, str], ...] = ()   # (table pair key, fk key) disambiguations
    keep_empty: bool = False              # LEFT JOIN: groups without related records count as 0
    limit_per: str | None = None          # top-N within each value of this column (window ranking)

    def with_(self, **changes: Any) -> Intent:
        return replace(self, **changes)


# ---- English rendering ------------------------------------------------------------

def entity(schema: Schema, table: str) -> str:
    """Singular entity name of a table: 'customers' -> 'customer'."""
    return " ".join(lemma(w) for w in schema.table(table).label.split())


def column_phrase(schema: Schema, key: str) -> str:
    """Readable name of a column in its table: 'district name', 'per pupil expenditure'.

    The (singular) table name is prefixed unless the column label already says
    which entity it belongs to.
    """
    column = schema.column(key)
    table = schema.table(column.table)
    name = entity(schema, table.name)
    label_lemmas = [lemma(w) for w in column.label.split()]
    entity_lemmas = name.split()
    if label_lemmas[: len(entity_lemmas)] == entity_lemmas:
        return column.label                      # 'singer name' in table singer
    # Keeping the entity on every phrase measurably helps Jev tell tables apart
    # (dropping it cost ~2 points on Spider train), even when slightly redundant.
    return f"{name} {column.label}"


# ---- building IR ------------------------------------------------------------------

def _effective_groups(intent: Intent, schema: Schema | None = None) -> tuple[str, ...]:
    """Explicit groups, or the groups implied by mixing plain values with aggregates.

    When the plain values describe individual records of one table (a name, a title),
    the group is that record: group by its primary key, so two airports that share a
    name are not merged.  Categorical values (a country) group by the value itself.
    """
    if intent.group_by:
        return intent.group_by
    has_agg = (any(o.is_aggregate for o in intent.outputs)
               or any(o.is_aggregate for o in intent.order)
               or any(c.kind in ("having", "having_vs") for c in intent.conditions))
    plain = tuple(dict.fromkeys(o.column for o in intent.outputs if not o.is_aggregate and o.column))
    if not (has_agg and plain):
        return ()
    if schema is not None:
        tables = {key.split(".", 1)[0] for key in plain}
        if len(tables) == 1:
            table = schema.table(next(iter(tables)))
            identifying = any(schema.unique_like(k) for k in plain)
            if identifying and len(table.primary_key) == 1:
                return (f"{table.name}.{table.primary_key[0]}",)
    return plain


@dataclass
class _Scope:
    schema: Schema
    aliases: dict[str, str] = field(default_factory=dict)   # table -> alias
    prefix: str = ""

    def alias(self, table: str) -> str:
        return self.aliases[table]

    def col(self, key: str) -> ir.Expr:
        column = self.schema.column(key)
        if column.formula is not None:
            op, left, right = column.formula
            if op in DATE_PARTS:
                return ir.Func("STRFTIME", (ir.Lit(DATE_PARTS[op]), self.col(left)))
            if op.startswith("since_"):
                reference = right.split(":", 1)[1]
                days = ir.BinOp("-", ir.Func("JULIANDAY", (ir.Lit(reference),)),
                                ir.Func("JULIANDAY", (self.col(left),)))
                divisor = ELAPSED_UNITS[op.removeprefix("since_")]
                return days if divisor == 1 else ir.Func("ROUND", (ir.BinOp("/", days, ir.Lit(divisor)),
                                                                   ir.Lit(1)))
            a, b = self.col(left), self.col(right)
            if op == "-" and self.schema.column(left).kind == DATE:
                a, b = ir.Func("JULIANDAY", (a,)), ir.Func("JULIANDAY", (b,))
            return ir.BinOp(op, a, b)
        return ir.Col(self.alias(self.schema.table(column.table).name), column.name)


def _from(schema: Schema, tables: list[str], root: str, prefix: str,
          join_choices: dict[str, str], left: bool = False) -> tuple[ir.From, _Scope]:
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
        joins.append(ir.Join(ir.TableRef(step.table, scope.alias(step.table)), on,
                             "LEFT" if left else "INNER"))
    return ir.From(ir.TableRef(root, scope.alias(root)), tuple(joins)), scope


def _agg_expr(scope: _Scope, func: str | None, column: str | None, table: str | None,
              distinct: bool = False) -> ir.Expr:
    if column is None:
        return ir.Agg("COUNT", ir.Star())
    expr = scope.col(column)
    return ir.Agg(func, expr, distinct) if func else expr


def _condition(schema: Schema, scope: _Scope, cond: Condition, counter: list[int],
               join_choices: dict[str, str]) -> ir.Cond:
    if cond.kind in ("all", "any"):
        parts = tuple(_condition(schema, scope, c, counter, join_choices) for c in cond.inner)
        return ir.And(parts) if cond.kind == "all" else ir.Or(parts)
    if cond.kind == "compare":
        value = cond.values[0]
        if schema.column(cond.col).kind == DATE and _is_year(value):
            # 'orders placed in 2024' / 'after 2020': compare the year of a date.
            return ir.Compare(ir.Func("STRFTIME", (ir.Lit("%Y"), scope.col(cond.col))), cond.op,
                              ir.Lit(str(int(value))))
        if schema.column(cond.col).kind == DATE and isinstance(value, str) and _MONTH.match(value):
            return ir.Compare(ir.Func("STRFTIME", (ir.Lit("%Y-%m"), scope.col(cond.col))), cond.op,
                              ir.Lit(value))
        return ir.Compare(scope.col(cond.col), cond.op, ir.Lit(value))
    if cond.kind == "columns":
        return ir.Compare(scope.col(cond.col), cond.op, scope.col(cond.values[0]))
    if cond.kind == "text":
        if cond.op == "not_contains":
            return ir.Like(scope.col(cond.col), f"%{cond.values[0]}%", negated=True)
        return ir.Like(scope.col(cond.col), TEXT_OPS[cond.op].format(cond.values[0]))
    if cond.kind == "period":
        column = scope.col(cond.col)
        return ir.And((ir.Compare(column, ">=", ir.Lit(cond.values[0])),
                       ir.Compare(column, "<", ir.Lit(cond.values[1]))))
    if cond.kind == "between":
        low, high = cond.values
        if schema.column(cond.col).kind == DATE and _is_year(low) and _is_year(high):
            return ir.Between(ir.Func("STRFTIME", (ir.Lit("%Y"), scope.col(cond.col))),
                              ir.Lit(str(low)), ir.Lit(str(high)))
        return ir.Between(scope.col(cond.col), ir.Lit(low), ir.Lit(high))
    if cond.kind == "in_values":
        return ir.InList(scope.col(cond.col), tuple(ir.Lit(v) for v in cond.values), cond.negated)
    if cond.kind == "null":
        test = ir.IsNull(scope.col(cond.col), cond.negated)
        if schema.column(cond.col).blank_count:
            # Empty text is 'missing' too ('players with an empty death record').
            blank = ir.Compare(ir.Func("TRIM", (scope.col(cond.col),)), "!=" if cond.negated else "=",
                               ir.Lit(""))
            return ir.And((test, blank)) if cond.negated else ir.Or((test, blank))
        return test
    if cond.kind == "having":
        expr = _agg_expr(scope, cond.func, cond.col, cond.table)
        return ir.Compare(expr, cond.op, ir.Lit(cond.values[0]))
    if cond.kind == "vs_group":
        counter[0] += 1
        table = cond.col.split(".", 1)[0]
        inner_from, inner_scope = _from(schema, list(schema.requires(cond.col)), table, f"q{counter[0]}_", join_choices)
        group = cond.table or ""
        sub = ir.Select(items=(ir.SelectItem(ir.Agg(cond.func or "AVG", inner_scope.col(cond.col))),),
                        from_=inner_from,
                        where=ir.Compare(inner_scope.col(group), "=", scope.col(group)))
        return ir.Compare(scope.col(cond.col), cond.op, ir.ScalarQuery(sub))
    if cond.kind == "top_group":
        counter[0] += 1
        table = cond.col.split(".", 1)[0]
        inner_from, inner_scope = _from(schema, list(schema.requires(cond.col)), table, f"q{counter[0]}_", join_choices)
        col = inner_scope.col(cond.col)
        sub = ir.Select(items=(ir.SelectItem(col),), from_=inner_from, group_by=(col,),
                        order_by=(ir.OrderItem(ir.Agg("COUNT", ir.Star()), cond.func != "ASC"),),
                        limit=1)
        return ir.Compare(scope.col(cond.col), cond.op, ir.ScalarQuery(sub))
    if cond.kind == "having_vs" and cond.column is None:
        # 'customers with more orders than the average customer': per-group count against
        # the overall number of counted records per subject record.
        counter[0] += 1
        prefix = f"q{counter[0]}_"
        counted, _ = _from(schema, [cond.table or ""], cond.table or "", prefix + "c", join_choices)
        subjects, _ = _from(schema, [cond.values[0]], cond.values[0], prefix + "s", join_choices)
        per_subject = ir.BinOp("/", ir.ScalarQuery(ir.Select((ir.SelectItem(ir.Agg("COUNT", ir.Star())),),
                                                             counted)),
                               ir.ScalarQuery(ir.Select((ir.SelectItem(ir.Agg("COUNT", ir.Star())),),
                                                        subjects)))
        return ir.Compare(ir.Agg("COUNT", ir.Star()), cond.op, per_subject)
    if cond.kind == "having_vs":
        counter[0] += 1
        table = cond.col.split(".", 1)[0]
        inner_from, inner_scope = _from(schema, list(schema.requires(cond.col)), table, f"q{counter[0]}_", join_choices)
        sub = ir.Select(items=(ir.SelectItem(ir.Agg(cond.func or "AVG", inner_scope.col(cond.col))),),
                        from_=inner_from)
        return ir.Compare(ir.Agg(cond.func or "AVG", scope.col(cond.col)), cond.op, ir.ScalarQuery(sub))
    if cond.kind == "vs_aggregate":
        counter[0] += 1
        table = cond.col.split(".", 1)[0]
        prefix = f"q{counter[0]}_"
        tables = list(schema.requires(cond.col)) + [t for c in cond.inner if c.column for t in schema.requires(c.column)]
        inner_from, inner_scope = _from(schema, tables, table, prefix, join_choices)
        where = ir.conjoin([_condition(schema, inner_scope, c, counter, join_choices)
                            for c in cond.inner])
        sub = ir.Select(items=(ir.SelectItem(ir.Agg(cond.func or "AVG", inner_scope.col(cond.col))),),
                        from_=inner_from, where=where)
        return ir.Compare(scope.col(cond.col), cond.op, ir.ScalarQuery(sub))
    if cond.kind in ("absent", "related"):
        return _membership(schema, scope, cond, counter, join_choices, negated=cond.kind == "absent")
    if cond.kind == "same_as":
        counter[0] += 1
        table = cond.col.split(".", 1)[0]
        tables = list(schema.requires(cond.col)) + [t for c in cond.inner if c.column for t in schema.requires(c.column)]
        inner_from, inner_scope = _from(schema, tables, table, f"q{counter[0]}_", join_choices)
        where = ir.conjoin([_condition(schema, inner_scope, c, counter, join_choices)
                            for c in cond.inner])
        sub = ir.Select(items=(ir.SelectItem(inner_scope.col(cond.col)),), from_=inner_from,
                        where=where)
        return ir.InQuery(scope.col(cond.col), sub)
    raise PlanError(f"Unsupported condition kind {cond.kind}")


_MONTH = re.compile(r"^\d{4}-\d{2}$")


def _is_year(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1000 <= value <= 2999


def _membership(schema: Schema, scope: _Scope, cond: Condition, counter: list[int],
                join_choices: dict[str, str], negated: bool) -> ir.Cond:
    subject = next(iter(scope.aliases))
    target = cond.table
    paths = schema.shortest_paths(subject, target, limit=1) if target else []
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
    where: list[ir.Cond] = [ir.IsNull(ir.Col(inner_scope.alias(first.table), link_col), negated=True)]
    where += [_condition(schema, inner_scope, c, counter, join_choices) for c in cond.inner]
    sub = ir.Select(items=(ir.SelectItem(ir.Col(inner_scope.alias(first.table), link_col)),),
                    from_=inner_from, where=ir.conjoin(where))
    test = ir.InQuery(ir.Col(scope.alias(subject), subject_col), sub, negated=negated)
    if negated and first.fk.table == subject and not schema.table(subject).column(subject_col).not_null:
        # 'employees with no manager': a missing reference (NULL) has no related record,
        # but NULL NOT IN (...) is unknown, not true.
        return ir.Or((ir.IsNull(ir.Col(scope.alias(subject), subject_col)), test))
    return test


def _fanned_out(schema: Schema, from_: ir.From, scope: _Scope) -> set[str]:
    """Tables whose rows are repeated by the joins (they are the 'one' side of a
    one-to-many join), so COUNT(*) would count them more than once."""
    by_alias = {alias: table for table, alias in scope.aliases.items()}
    fanned: set[str] = set()
    for join in from_.joins:
        for left, right in join.on:
            for a, b in ((left, right), (right, left)):
                ta, tb = by_alias.get(a.source), by_alias.get(b.source)
                if ta is None or tb is None:
                    continue
                # ``ta`` is repeated when ``tb`` references it (tb holds the FK to ta's key).
                if any(fk.table == tb and fk.ref_table == ta and b.column in fk.columns
                       for fk in schema.foreign_keys):
                    fanned.add(ta)
    return fanned


def _count_expr(schema: Schema, scope: _Scope, table: str | None, fanned: set[str],
                outer: bool = False) -> ir.Expr:
    """COUNT(*) of a table, or COUNT(DISTINCT key) when joins repeat its rows; under an
    outer join, the count of the table's non-null key (a missing match counts as 0)."""
    if outer and table and table != next(iter(scope.aliases)):
        t = schema.table(table)
        column = t.primary_key[0] if t.primary_key else t.columns[0].name
        return ir.Agg("COUNT", ir.Col(scope.alias(table), column), table in fanned and bool(t.primary_key))
    if table in fanned:
        pk = schema.table(table).primary_key
        if len(pk) == 1 and len(fanned) >= 1 and len(scope.aliases) > 1:
            return ir.Agg("COUNT", ir.Col(scope.alias(table), pk[0]), True)
    return ir.Agg("COUNT", ir.Star())


def query_tables(intent: Intent, schema: Schema | None = None) -> list[str]:
    """Tables the main query joins; with a schema, also the operand tables of derived measures."""
    tables: list[str] = []

    def add(key: str | None, fallback: tuple[str, ...] = ()) -> None:
        if key and schema is not None:
            tables.extend(schema.requires(key))
        elif key:
            tables.append(key.split(".", 1)[0])
        tables.extend(fallback)

    for item in intent.outputs:
        if item.per:
            tables.append(item.per)
        add(item.column, () if item.column else item.tables)
        for cond in item.where:
            add(cond.column)
    for cond in intent.conditions:
        if not cond.tables:
            continue
        if cond.column:
            add(cond.column)
            if cond.kind == "columns":
                add(cond.values[0])
        else:
            tables.extend(cond.tables)
    for key in intent.group_by:
        add(key)
    tables.extend(intent.via)
    for order in intent.order:
        if order.column:
            add(order.column)
        elif order.table:
            tables.append(order.table)
    return list(dict.fromkeys(tables))


def build_query(schema: Schema, intent: Intent) -> ir.Query:
    if not intent.outputs:
        raise PlanError("Nothing to return")
    join_choices = dict(intent.join_choices)
    if any(o.per for o in intent.outputs):
        return _nested(schema, intent, join_choices)
    if intent.limit_per and intent.order and intent.limit:
        return _ranked(schema, intent, join_choices)
    having = [c for c in intent.conditions if c.kind == "having"]

    def source(c: Condition) -> str:
        return c.col.split(".", 1)[0] if c.column else c.table or ""

    sources = list(dict.fromkeys(source(c) for c in having))
    if len(sources) >= 2 and not intent.order and (intent.group_by or _effective_groups(intent, schema)):
        # Aggregates over different related tables cannot share one join (each table's
        # rows would multiply the other's): evaluate each separately, combine the groups.
        rest = [c for c in intent.conditions if c.kind != "having"]
        parts = [intent.with_(conditions=tuple(rest + [c for c in having if source(c) == t]))
                 for t in sources]
        op = "UNION" if intent.connector == "OR" else "INTERSECT"
        query: ir.Query = _select(schema, parts[0], "p0_", join_choices)
        for i, part in enumerate(parts[1:], start=1):
            query = ir.SetOp(op, query, _select(schema, part, f"p{i}_", join_choices))
        return query
    having = [c for c in intent.conditions if c.kind in ("having", "having_vs")]
    if having and not intent.group_by and not _effective_groups(intent, schema) and intent.subject:
        # 'How many students have at least one B grade': count the students whose key is
        # among those satisfying the per-student condition.
        subject = intent.subject
        pk = schema.table(subject).primary_key
        if len(pk) == 1:
            key = f"{schema.table(subject).name}.{pk[0]}"
            groups = intent.with_(outputs=(OutputItem(None, key),), group_by=(key,), order=(),
                                  limit=None, offset=None, distinct=False)
            outer = intent.with_(conditions=tuple(c for c in intent.conditions if c not in having))
            inner = _select(schema, groups, "h_", join_choices)
            return _select(schema, outer, "", join_choices,
                           extra_where=lambda scope: ir.InQuery(scope.col(key), inner))
    keyed = (any(o.is_aggregate for o in intent.outputs) or intent.order or intent.group_by
             or intent.limit is not None)
    if intent.excluded:
        kept = intent.with_(excluded=())
        removed = intent.with_(excluded=(), conditions=intent.excluded, both=None, order=(),
                               limit=None, offset=None, distinct=False, group_by=())
        if keyed or _lists_records(schema, intent):
            # Records are removed by key: two customers who share a name stay two rows.
            # Remove records by key, then aggregate/order what remains.
            return _with_key_set(schema, kept, [(removed, "x_")], "EXCEPT", join_choices)
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
        if keyed:
            main = intent.with_(conditions=tuple(shared), both=None)
            return _with_key_set(schema, main, [(left, "a_"), (right, "b_")], "INTERSECT",
                                 join_choices)
        return ir.SetOp("INTERSECT", _select(schema, left, "a_", join_choices),
                        _select(schema, right, "b_", join_choices))
    semi = (_semi_join(schema, intent, join_choices) or _semi_join_measures(schema, intent, join_choices)
            if SEMI_JOIN_FILTERS else None)
    return semi or _select(schema, intent, "", join_choices)


def _semi_join_measures(schema: Schema, intent: Intent, join_choices: dict[str, str]) -> ir.Select | None:
    """'the total of orders that contain a Latte': a filter through an order's lines must
    not add the order total once per matching line.  Filters on tables related one-to-many
    to the summed table become a membership test on its key."""
    measured = [o for o in intent.outputs if o.func in ("SUM", "AVG") and o.column and not o.per]
    if not measured or intent.connector != "AND" or intent.both is not None or intent.excluded:
        return None
    hosts = {t for o in measured for t in schema.requires(o.column or "")}
    if len(hosts) != 1:
        return None
    table = next(iter(hosts))
    pk = schema.table(table).primary_key
    if len(pk) != 1:
        return None
    moved = [c for c in intent.conditions if c.kind not in ("having", "having_vs") and c.tables
             and all(t != table and schema.one_to_many(table, t) for t in c.tables)]
    if not moved:
        return None
    key = f"{schema.table(table).name}.{pk[0]}"
    inner = _select(schema, intent.with_(outputs=(OutputItem(None, key),), conditions=tuple(moved), group_by=(),
                                         order=(), limit=None, offset=None, distinct=False, subject=table),
                    "m_", join_choices)
    outer = intent.with_(conditions=tuple(c for c in intent.conditions if c not in moved))
    return _select(schema, outer, "", join_choices, extra_where=lambda scope: ir.InQuery(scope.col(key), inner))


def _semi_join(schema: Schema, intent: Intent, join_choices: dict[str, str]) -> ir.Select | None:
    """'Which regions have stores that sold Croissants?' lists each region once.

    Filtering through related records on the 'many' side of a join repeats the listed
    records once per match; such conditions become a membership test on the listed
    table's key instead (two different records with equal names still stay two rows)."""
    if (intent.group_by or intent.distinct or intent.connector != "AND"
            or any(o.is_aggregate or o.star for o in intent.outputs)
            or any(o.is_aggregate for o in intent.order)
            or any(c.kind in ("having", "having_vs") for c in intent.conditions)):
        return None
    listed = {t for o in intent.outputs for t in o.tables}
    if len(listed) != 1:
        return None
    table = next(iter(listed))
    pk = schema.table(table).primary_key
    if len(pk) != 1:
        return None
    moved = [c for c in intent.conditions if c.tables and any(
        t != table and schema.one_to_many(table, t) for t in c.tables)]
    order_tables = {o.column.split(".", 1)[0] if o.column else o.table for o in intent.order}
    if not moved or any(t != table and schema.one_to_many(table, t) for t in order_tables if t):
        return None
    key = f"{schema.table(table).name}.{pk[0]}"
    inner = _select(schema, intent.with_(outputs=(OutputItem(None, key),), conditions=tuple(moved),
                                         order=(), limit=None, offset=None, subject=table), "s_",
                    join_choices)
    outer = intent.with_(conditions=tuple(c for c in intent.conditions if c not in moved), subject=table)
    return _select(schema, outer, "", join_choices,
                   extra_where=lambda scope: ir.InQuery(scope.col(key), inner))


def _lists_records(schema: Schema, intent: Intent) -> bool:
    """True when the outputs identify records of the subject (a key or a near-unique name)."""
    subject = intent.subject
    if not subject or len(schema.table(subject).primary_key) != 1:
        return False
    for item in intent.outputs:
        if item.column and not item.is_aggregate and item.column.split(".", 1)[0] == subject:
            column = schema.column(item.column)
            if schema.unique_like(item.column):
                return True
            if re.search(r"\b(name|title)\b", column.label):
                return True     # a record's label: two customers named Ann Lee are two customers
    return False


def _negate(cond: Condition) -> Condition:
    flip = {"=": "!=", "!=": "=", ">": "<=", "<": ">=", ">=": "<", "<=": ">",
            "contains": "not_contains", "not_contains": "contains"}
    if cond.kind in ("compare", "text") and cond.op in flip:
        kind = "text" if flip[cond.op] in ("contains", "not_contains") else cond.kind
        return Condition(kind, cond.column, flip[cond.op], cond.values)
    if cond.kind in ("in_values", "null"):
        return Condition(cond.kind, cond.column, cond.op, cond.values, not cond.negated)
    raise PlanError("This exclusion cannot be expressed without a record key")


def _with_key_set(schema: Schema, main: Intent, parts: list[tuple[Intent, str]], op: str,
                  join_choices: dict[str, str]) -> ir.Query:
    """``main`` restricted by a set operation over the subject's key:
    INTERSECT: key IN (A INTERSECT B); EXCEPT: key NOT IN (removed)."""
    subject = main.subject or next(iter(query_tables(main)), None)
    if subject is None:
        raise PlanError("No subject for a set operation")
    pk = schema.table(subject).primary_key
    if len(pk) != 1:
        if op == "EXCEPT":
            # No key to remove records by: exclude matching rows directly instead.
            negated = tuple(_negate(c) for c in parts[0][0].conditions)
            return _select(schema, main.with_(conditions=main.conditions + negated), "", join_choices)
        raise PlanError(f"{subject} has no single-column key to combine results by")
    key = OutputItem(None, f"{schema.table(subject).name}.{pk[0]}")

    def key_select(part: Intent, prefix: str) -> ir.Select:
        return _select(schema, part.with_(outputs=(key,), order=(), limit=None, offset=None,
                                          group_by=(), distinct=False, both=None, excluded=(),
                                          subject=subject), prefix, join_choices)

    if op == "EXCEPT":
        subquery: ir.Query = key_select(*parts[0])
        exclude = True
    else:
        subquery = key_select(*parts[0])
        for part, prefix in parts[1:]:
            subquery = ir.SetOp(op, subquery, key_select(part, prefix))
        exclude = False
    key_column = key.column or ""
    return _select(schema, main.with_(subject=subject), "", join_choices,
                   extra_where=lambda scope: ir.InQuery(scope.col(key_column), subquery, exclude))


def output_name(schema: Schema, item: OutputItem) -> str | None:
    """A readable result-column name for computed outputs (plain columns keep theirs)."""
    if item.star:
        return None
    if item.func == "PCT":
        return f"percentage of {schema.table(item.table or '').label}"
    if item.column is None and item.per:
        return (f"{FUNC_WORDS[item.func or 'AVG']} number of {schema.table(item.table or '').label} per "
                f"{entity(schema, item.per)}")
    if item.column is None:
        return f"number of {schema.table(item.table or '').label}"
    column = schema.column(item.column)
    if item.func is None:
        return column.label if column.formula else None
    if item.func == "COUNT":
        return f"number of {'distinct ' if item.distinct else ''}{column.label}"
    if item.per:
        return f"{FUNC_WORDS[item.func or 'AVG']} {column.label} per {entity(schema, item.per)}"
    return f"{FUNC_WORDS[item.func]} {column.label}"


def _having(schema: Schema, scope: _Scope, cond: Condition, counter: list[int],
            join_choices: dict[str, str], fanned: set[str]) -> ir.Cond:
    if cond.kind == "having" and not cond.column and cond.func == "COUNT":
        return ir.Compare(_count_expr(schema, scope, cond.table, fanned), cond.op, ir.Lit(cond.values[0]))
    result = _condition(schema, scope, cond, counter, join_choices)
    if cond.kind == "having_vs" and cond.column is None and isinstance(result, ir.Compare):
        return ir.Compare(_count_expr(schema, scope, cond.table, fanned), result.op, result.right)
    return result


def _select(schema: Schema, intent: Intent, prefix: str, join_choices: dict[str, str],
            extra_where: Any = None) -> ir.Select:
    tables = query_tables(intent, schema)
    root = intent.subject if intent.subject in tables else (tables[0] if tables else intent.subject)
    if root is None:
        raise PlanError("No source table")
    if intent.subject and intent.subject not in tables:
        tables = [intent.subject] + tables
        root = intent.subject
    outer = intent.keep_empty
    from_, scope = _from(schema, tables, root, prefix, join_choices, left=outer)
    counter = [0]
    fanned = _fanned_out(schema, from_, scope)

    def output(o: OutputItem) -> ir.Expr:
        if o.star:
            return ir.Star(scope.alias(o.table or ""))
        if o.func == "PCT":
            # 'what percentage of orders were cancelled': 100 * matching / all counted rows.
            match = ir.conjoin([_condition(schema, scope, c, counter, join_choices) for c in o.where])
            hits = ir.Agg("SUM", ir.Case(((match, ir.Lit(1)),), ir.Lit(0))) if match else ir.Lit(0)
            return ir.BinOp("*", ir.Lit(100.0), ir.BinOp("/", hits, _count_expr(
                schema, scope, o.table, fanned, outer)))
        if o.func == "COUNT" and o.column is None:
            return _count_expr(schema, scope, o.table, fanned, outer)
        return _agg_expr(scope, o.func, o.column, o.table, o.distinct)

    items = tuple(ir.SelectItem(output(o), output_name(schema, o)) for o in intent.outputs)
    where_conds = [_condition(schema, scope, c, counter, join_choices)
                   for c in intent.conditions if c.kind not in ("having", "having_vs")
                   and not (c.kind == "related" and c.table in scope.aliases)]
    if intent.connector == "OR" and len(where_conds) > 1:
        where: ir.Cond | None = ir.Or(tuple(where_conds))
    else:
        where = ir.conjoin(where_conds)
    if extra_where is not None:
        where = ir.conjoin(([where] if where is not None else []) + [extra_where(scope)])
    having_conds = [_having(schema, scope, c, counter, join_choices, fanned)
                    for c in intent.conditions if c.kind in ("having", "having_vs")]
    groups = tuple(scope.col(key) for key in _effective_groups(intent, schema))
    order = tuple(ir.OrderItem(_count_expr(schema, scope, o.table, fanned, outer)
                               if o.func == "COUNT" and o.column is None else
                               _agg_expr(scope, o.func, o.column, o.table), o.desc)
                  for o in intent.order)
    return ir.Select(items=items, from_=from_, where=where, group_by=groups,
                     having=ir.conjoin(having_conds), order_by=order, limit=intent.limit,
                     offset=intent.offset, distinct=intent.distinct)


def _nested(schema: Schema, intent: Intent, join_choices: dict[str, str]) -> ir.Select:
    """Aggregates of per-record aggregates, through a FROM subquery.

    'the average order value': AVG over orders of each order's SUM(quantity * price);
    'the average number of orders per customer': AVG over customers of each one's
    COUNT(orders), counting customers without orders as 0 (an outer join)."""
    nested = [o for o in intent.outputs if o.per]
    per = nested[0].per or ""
    if any(o.per != per for o in nested) or any(not o.per and o.is_aggregate for o in intent.outputs):
        raise PlanError("Nested aggregates over different record kinds are not supported")
    pk = schema.table(per).primary_key
    if len(pk) != 1:
        raise PlanError(f"{per} has no single-column key to aggregate per record")
    key = f"{schema.table(per).name}.{pk[0]}"
    groups = [o.column for o in intent.outputs if not o.is_aggregate and o.column]
    inner_outputs = tuple([OutputItem(None, g) for g in groups] +
                          [OutputItem("SUM", o.column) if o.column else OutputItem("COUNT", None, o.table)
                           for o in nested])
    inner_intent = intent.with_(outputs=inner_outputs, group_by=tuple(dict.fromkeys([key] + groups)),
                                order=(), limit=None, offset=None, distinct=False, subject=per,
                                keep_empty=True, limit_per=None)
    inner = _select(schema, inner_intent, "n_", join_choices)
    names = [f"g{i}" for i in range(len(groups))] + [f"v{i}" for i in range(len(nested))]
    inner = replace(inner, items=tuple(ir.SelectItem(item.expr, name)
                                       for item, name in zip(inner.items, names)))
    alias = "nested"
    items: list[ir.SelectItem] = []
    g = v = 0
    for o in intent.outputs:
        if o.per:
            items.append(ir.SelectItem(ir.Agg(o.func or "AVG", ir.Col(alias, f"v{v}")), output_name(schema, o)))
            v += 1
        elif o.column:
            items.append(ir.SelectItem(ir.Col(alias, f"g{g}"), schema.column(o.column).label))
            g += 1
    order = []
    for ordering in intent.order:
        for i, n in enumerate(nested):
            if (ordering.func, ordering.column, ordering.table) in ((n.func, n.column, n.table),
                                                                   ("SUM", n.column, None)):
                order.append(ir.OrderItem(ir.Agg(n.func or "AVG", ir.Col(alias, f"v{i}")), ordering.desc))
    return ir.Select(items=tuple(items), from_=ir.From(ir.Derived(inner, alias)),
                     group_by=tuple(ir.Col(alias, f"g{i}") for i in range(len(groups))),
                     order_by=tuple(order), limit=intent.limit if order else None)


def _ranked(schema: Schema, intent: Intent, join_choices: dict[str, str]) -> ir.Select:
    """Top-N within each group: 'the two cheapest products in each category'."""
    partition = intent.limit_per or ""
    ordered = _select(schema, intent.with_(outputs=intent.outputs + (OutputItem(None, partition),),
                                           limit=None, offset=None, limit_per=None), "r_", join_choices)
    inner = replace(ordered, order_by=())
    names = [f"c{i}" for i in range(len(inner.items))]
    rank = ir.Window("ROW_NUMBER", (inner.items[-1].expr,), ordered.order_by)
    inner = replace(inner, items=tuple(ir.SelectItem(item.expr, name) for item, name in zip(inner.items, names))
                    + (ir.SelectItem(rank, "position"),))
    alias = "ranked"
    labels = [output_name(schema, o) or (schema.column(o.column).label if o.column else f"c{i}")
              for i, o in enumerate(intent.outputs)]
    return ir.Select(items=tuple(ir.SelectItem(ir.Col(alias, names[i]), labels[i])
                                 for i in range(len(intent.outputs))),
                     from_=ir.From(ir.Derived(inner, alias)),
                     where=ir.Compare(ir.Col(alias, "position"), "<=", ir.Lit(intent.limit or 1)),
                     order_by=(ir.OrderItem(ir.Col(alias, names[-1])), ir.OrderItem(ir.Col(alias, "position"))))
