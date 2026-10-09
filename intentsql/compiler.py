"""Deterministic IR -> SQLite compiler and structural validator.

Values are always bound parameters.  Identifiers are taken from the grounded
schema and quoted when needed.  Plans that reference unknown objects or break
SQL structure rules are rejected with explicit issues; nothing is guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from intentsql import ir
from intentsql.ir import (Agg, And, Between, BinOp, Case, Col, Compare, Exists, Func, InList,
                          InQuery, IsNull, Like, Lit, Not, Or, Query, ScalarQuery, Select, SetOp,
                          Star, ValidationIssue)
from intentsql.schema import NUMBER, Column, Schema

_SIMPLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KEYWORDS = frozenset("""
abort action add after all alter analyze and as asc attach autoincrement before begin between
by cascade case cast check collate column commit conflict constraint create cross current
current_date current_time current_timestamp database default deferrable deferred delete desc
detach distinct drop each else end escape except exclusive exists explain fail for foreign
from full glob group having if ignore immediate in index indexed initially inner insert instead
intersect into is isnull join key left like limit match natural no not notnull null of offset
on or order outer plan pragma primary query raise recursive references regexp reindex release
rename replace restrict right rollback row savepoint select set table temp temporary then to
transaction trigger union unique update using vacuum values view virtual when where with
without year date time
""".split())


class CompileError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))


@dataclass(frozen=True)
class CompiledSQL:
    sql: str
    params: tuple[Any, ...]
    display_sql: str


def quote(name: str) -> str:
    if _SIMPLE.match(name) and name.lower() not in _KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def _display_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


@dataclass
class _Scope:
    refs: dict[str, str]               # alias -> table name
    parent: "_Scope | None" = None

    def resolve(self, alias: str) -> str | None:
        scope: _Scope | None = self
        while scope is not None:
            if alias in scope.refs:
                return scope.refs[alias]
            scope = scope.parent
        return None


@dataclass
class _Compiler:
    schema: Schema
    aliases: dict[str, str] = field(default_factory=dict)   # IR alias -> SQL alias
    use_aliases: bool = False
    params: list[Any] = field(default_factory=list)
    display_params: list[str] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    # -- issues ---------------------------------------------------------------
    def fail(self, code: str, message: str, node: Any = None) -> None:
        self.issues.append(ValidationIssue(code, message, node))

    # -- columns ----------------------------------------------------------------
    def column(self, col: Col, scope: _Scope) -> Column | None:
        table = scope.resolve(col.source)
        if table is None:
            self.fail("unknown_alias", f"Column {col.column} refers to unknown source {col.source}", col)
            return None
        try:
            return self.schema.table(table).column(col.column)
        except KeyError:
            self.fail("unknown_column", f"Table {table} has no column {col.column}", col)
            return None

    def coerce(self, value: Any, column: Column | None) -> Any:
        """Bind a literal with the storage class of the column it is compared to."""
        if column is None or value is None or isinstance(value, bool):
            return value
        if column.storage in ("integer", "real") or (column.kind == NUMBER and column.storage != "text"):
            if isinstance(value, str):
                text = value.strip().replace(",", "")
                try:
                    number = float(text)
                except ValueError:
                    return value
                return int(number) if number.is_integer() and "." not in text else number
            return value
        if column.storage == "text" and isinstance(value, (int, float)):
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)
        return value

    def bind(self, value: Any) -> str:
        self.params.append(value)
        self.display_params.append(_display_literal(value))
        return "?"

    # -- expressions -----------------------------------------------------------
    def expr(self, node: Any, scope: _Scope, *, context: Column | None = None,
             allow_agg: bool = True) -> str:
        if isinstance(node, Col):
            column = self.column(node, scope)
            name = quote(column.name if column else node.column)
            if self.use_aliases:
                return f"{self.aliases.get(node.source, node.source)}.{name}"
            return name
        if isinstance(node, Star):
            if node.source and self.use_aliases:
                return f"{self.aliases.get(node.source, node.source)}.*"
            return "*"
        if isinstance(node, Lit):
            return self.bind(self.coerce(node.value, context))
        if isinstance(node, Agg):
            if not allow_agg:
                self.fail("aggregate_not_allowed", f"{node.func} is not allowed here", node)
            if node.func not in ir.AGGREGATES:
                self.fail("bad_aggregate", f"Unknown aggregate {node.func}", node)
            if ir.has_aggregate(node.arg):
                self.fail("nested_aggregate", "Aggregates cannot be nested", node)
            if isinstance(node.arg, Star) and node.func != "COUNT":
                self.fail("bad_aggregate", f"{node.func}(*) is not valid", node)
            inner = self.expr(node.arg, scope, allow_agg=False)
            return f"{node.func}({'DISTINCT ' if node.distinct else ''}{inner})"
        if isinstance(node, BinOp):
            if node.op not in ir.ARITHMETIC:
                self.fail("bad_operator", f"Unknown arithmetic operator {node.op}", node)
            left = self.expr(node.left, scope, allow_agg=allow_agg)
            right = self.expr(node.right, scope, allow_agg=allow_agg)
            if node.op == "/":  # SQLite integer division truncates; divide as REAL.
                left = f"CAST({left} AS REAL)"
            return f"({left} {node.op} {right})"
        if isinstance(node, Func):
            bounds = ir.FUNCTIONS.get(node.name)
            if bounds is None or not bounds[0] <= len(node.args) <= bounds[1]:
                self.fail("bad_function", f"Function {node.name} is not supported with "
                          f"{len(node.args)} argument(s)", node)
            args = [self.expr(a, scope, allow_agg=allow_agg) for a in node.args]
            if node.name == "CAST_REAL":
                return f"CAST({args[0]} AS REAL)"
            if node.name == "CAST_INTEGER":
                return f"CAST({args[0]} AS INTEGER)"
            return f"{node.name}({', '.join(args)})"
        if isinstance(node, Case):
            parts = ["CASE"]
            for cond, value in node.whens:
                parts.append(f"WHEN {self.cond(cond, scope, allow_agg=allow_agg)} "
                             f"THEN {self.expr(value, scope, allow_agg=allow_agg)}")
            if node.otherwise is not None:
                parts.append(f"ELSE {self.expr(node.otherwise, scope, allow_agg=allow_agg)}")
            parts.append("END")
            return " ".join(parts)
        if isinstance(node, ScalarQuery):
            width = _width(node.query)
            if width != 1:
                self.fail("subquery_width", "A scalar subquery must return one column", node)
            return f"({self.query(node.query, scope)})"
        self.fail("bad_expression", f"Unsupported expression {type(node).__name__}", node)
        return "NULL"

    def _context(self, node: Any, scope: _Scope) -> Column | None:
        if isinstance(node, Col):
            return self.column(node, scope)
        return None

    # -- conditions -----------------------------------------------------------
    def cond(self, node: Any, scope: _Scope, *, allow_agg: bool = False) -> str:
        if isinstance(node, Compare):
            if node.op not in ir.COMPARISONS:
                self.fail("bad_operator", f"Unknown comparison {node.op}", node)
            context = self._context(node.left, scope) or self._context(node.right, scope)
            left = self.expr(node.left, scope, context=context, allow_agg=allow_agg)
            right = self.expr(node.right, scope, context=context, allow_agg=allow_agg)
            return f"{left} {node.op} {right}"
        if isinstance(node, Between):
            context = self._context(node.expr, scope)
            return (f"{self.expr(node.expr, scope, allow_agg=allow_agg)} "
                    f"{'NOT ' if node.negated else ''}BETWEEN "
                    f"{self.expr(node.low, scope, context=context, allow_agg=allow_agg)} AND "
                    f"{self.expr(node.high, scope, context=context, allow_agg=allow_agg)}")
        if isinstance(node, InList):
            if not node.values:
                self.fail("empty_in", "IN requires at least one value", node)
            context = self._context(node.expr, scope)
            values = ", ".join(self.expr(v, scope, context=context) for v in node.values)
            return (f"{self.expr(node.expr, scope, allow_agg=allow_agg)} "
                    f"{'NOT ' if node.negated else ''}IN ({values})")
        if isinstance(node, InQuery):
            if _width(node.query) != 1:
                self.fail("subquery_width", "IN (subquery) must return one column", node)
            return (f"{self.expr(node.expr, scope, allow_agg=allow_agg)} "
                    f"{'NOT ' if node.negated else ''}IN ({self.query(node.query, scope)})")
        if isinstance(node, Exists):
            return f"{'NOT ' if node.negated else ''}EXISTS ({self.query(node.query, scope)})"
        if isinstance(node, Like):
            if not isinstance(node.pattern, str):
                self.fail("bad_pattern", "LIKE pattern must be text", node)
            return (f"{self.expr(node.expr, scope, allow_agg=allow_agg)} "
                    f"{'NOT ' if node.negated else ''}LIKE {self.bind(node.pattern)}")
        if isinstance(node, IsNull):
            return (f"{self.expr(node.expr, scope, allow_agg=allow_agg)} "
                    f"IS {'NOT ' if node.negated else ''}NULL")
        if isinstance(node, (And, Or)):
            if not node.items:
                self.fail("empty_boolean", "Empty AND/OR", node)
                return "1"
            joiner = " AND " if isinstance(node, And) else " OR "
            parts = []
            for item in node.items:
                text = self.cond(item, scope, allow_agg=allow_agg)
                if isinstance(item, (And, Or)) and type(item) is not type(node):
                    text = f"({text})"
                parts.append(text)
            return joiner.join(parts)
        if isinstance(node, Not):
            return f"NOT ({self.cond(node.item, scope, allow_agg=allow_agg)})"
        self.fail("bad_condition", f"Unsupported condition {type(node).__name__}", node)
        return "1"

    # -- queries ----------------------------------------------------------------
    def query(self, node: Query, parent: _Scope | None = None) -> str:
        if isinstance(node, SetOp):
            if node.op not in ir.SET_OPERATORS:
                self.fail("bad_set_operator", f"Unknown set operator {node.op}", node)
            if _width(node.left) != _width(node.right):
                self.fail("set_width_mismatch", "Set operation sides return different widths", node)
            left = self.query(node.left, parent)
            right = self.query(node.right, parent)
            sql = f"{left} {node.op} {right}"
            if node.order_by or node.limit is not None:
                self.fail("set_op_order", "Ordering a compound query is not supported", node)
            return sql
        if not isinstance(node, Select):
            self.fail("bad_query", f"Unsupported query node {type(node).__name__}")
            return "SELECT NULL"
        refs: dict[str, str] = {}
        for ref in node.from_.refs:
            try:
                table = self.schema.table(ref.table).name
            except KeyError:
                self.fail("unknown_table", f"Unknown table {ref.table}", ref)
                table = ref.table
            if ref.alias in refs or (parent and parent.resolve(ref.alias)):
                self.fail("duplicate_alias", f"Alias {ref.alias} is used twice", ref)
            refs[ref.alias] = table
        scope = _Scope(refs, parent)
        if not node.items:
            self.fail("empty_select", "A query must select at least one expression", node)
        items = []
        for item in node.items:
            text = self.expr(item.expr, scope)
            if item.alias:
                text += f" AS {quote(item.alias)}"
            items.append(text)
        sql = "SELECT " + ("DISTINCT " if node.distinct else "") + ", ".join(items)
        sql += " FROM " + self._table(node.from_.base)
        for join in node.from_.joins:
            if not join.on:
                self.fail("join_without_condition", f"Join to {join.right.table} has no condition", join)
            keyword = "LEFT JOIN" if join.kind == "LEFT" else "JOIN"
            on = " AND ".join(f"{self.expr(a, scope)} = {self.expr(b, scope)}" for a, b in join.on)
            sql += f" {keyword} {self._table(join.right)} ON {on}"
        if node.where is not None:
            if ir.has_aggregate(node.where):
                self.fail("aggregate_in_where", "Aggregates belong in HAVING, not WHERE", node.where)
            sql += " WHERE " + self.cond(node.where, scope)
        if node.group_by:
            sql += " GROUP BY " + ", ".join(self.expr(g, scope, allow_agg=False) for g in node.group_by)
        if node.having is not None:
            if not node.group_by and not ir.has_aggregate(node.having):
                self.fail("having_without_group", "HAVING needs GROUP BY or an aggregate", node)
            sql += " HAVING " + self.cond(node.having, scope, allow_agg=True)
        if node.order_by:
            sql += " ORDER BY " + ", ".join(
                f"{self.expr(o.expr, scope)}{' DESC' if o.desc else ' ASC'}" for o in node.order_by)
        sql += self._limit(node.limit, node.offset)
        return sql

    def _limit(self, limit: int | None, offset: int | None) -> str:
        text = ""
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                self.fail("bad_limit", f"LIMIT must be a non-negative integer, got {limit!r}")
                return ""
            text += f" LIMIT {limit}"
        if offset:
            if not isinstance(offset, int) or offset < 0:
                self.fail("bad_offset", f"OFFSET must be a non-negative integer, got {offset!r}")
                return text
            if limit is None:
                text += " LIMIT -1"
            text += f" OFFSET {offset}"
        return text

    def _table(self, ref: ir.TableRef) -> str:
        try:
            name = quote(self.schema.table(ref.table).name)
        except KeyError:
            name = quote(ref.table)
        if self.use_aliases:
            return f"{name} AS {self.aliases[ref.alias]}"
        return name


def _width(query: Query) -> int:
    while isinstance(query, SetOp):
        query = query.left
    return len(query.items) if all(not isinstance(i.expr, Star) for i in query.items) else -1


def _all_refs(query: Any) -> list[ir.TableRef]:
    return [node for node in ir.walk(query) if isinstance(node, ir.TableRef)]


def compile_query(query: Query, schema: Schema) -> CompiledSQL:
    refs = _all_refs(query)
    compiler = _Compiler(schema)
    if len(refs) > 1:
        compiler.use_aliases = True
        for index, ref in enumerate(refs, start=1):
            if ref.alias in compiler.aliases:
                compiler.fail("duplicate_alias", f"Alias {ref.alias} is used twice", ref)
            compiler.aliases[ref.alias] = f"T{index}"
    sql = compiler.query(query)
    if compiler.issues:
        raise CompileError(compiler.issues)
    display = _render_display(sql, compiler.display_params)
    return CompiledSQL(sql, tuple(compiler.params), display)


def _render_display(sql: str, rendered: list[str]) -> str:
    """Substitute rendered literals for ``?`` outside of quoted identifiers."""
    out, index, in_quote = [], 0, False
    for char in sql:
        if char == '"':
            in_quote = not in_quote
        if char == "?" and not in_quote and index < len(rendered):
            out.append(rendered[index])
            index += 1
        else:
            out.append(char)
    return "".join(out)
