"""Deterministic schema grounding for an arbitrary SQLite database.

Everything the interpreter may reference -- tables, columns, types, keys,
relationships and join paths -- comes from here.  Jev never names an
identifier; it only selects among objects built by this module.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Iterable

from intentsql.text import humanize

NUMBER, TEXT, DATE, BOOLEAN, OTHER = "number", "text", "date", "boolean", "other"

_ISO_DATE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?)?$")
_NUMERIC = re.compile(r"^\s*[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?\s*$")
_IDENTIFIER = re.compile(r"(^|_)[Ii][Dd]$|[a-z]I[Dd]$|ID$")
VALUE_INDEX_LIMIT = 5000
SAMPLE_SIZE = 300


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Column:
    table: str
    name: str
    declared_type: str
    kind: str                 # number | text | date | boolean | other
    primary_key: bool
    not_null: bool
    label: str                # human words, e.g. "song release year"
    distinct_count: int | None = None
    null_count: int | None = None
    storage: str = "text"     # dominant SQLite storage class of stored values

    @property
    def key(self) -> str:
        return f"{self.table}.{self.name}"

    @property
    def is_numeric(self) -> bool:
        return self.kind == NUMBER

    @property
    def is_identifier(self) -> bool:
        return self.primary_key or bool(_IDENTIFIER.search(self.name))


@dataclass(frozen=True)
class ForeignKey:
    table: str
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]
    declared: bool = True

    def other(self, table: str) -> str:
        return self.ref_table if table == self.table else self.table

    def pairs_from(self, table: str) -> tuple[tuple[str, str], ...]:
        """Column pairs oriented (column in ``table``, column in the other table)."""
        pairs = tuple(zip(self.columns, self.ref_columns))
        if table == self.table:
            return pairs
        return tuple((b, a) for a, b in pairs)

    @property
    def key(self) -> str:
        return f"{self.table}({','.join(self.columns)})->{self.ref_table}({','.join(self.ref_columns)})"


@dataclass(frozen=True)
class Table:
    name: str
    label: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    row_count: int

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        for column in self.columns:
            if column.name.lower() == name.lower():
                return column
        raise KeyError(f"{self.name}.{name}")


@dataclass(frozen=True)
class JoinStep:
    """Join ``table`` to the tree through ``fk``; pairs are (tree column, new column)."""
    table: str
    from_table: str
    fk: ForeignKey

    @property
    def pairs(self) -> tuple[tuple[str, str], ...]:
        return self.fk.pairs_from(self.from_table)


@dataclass
class Schema:
    path: str
    tables: dict[str, Table]
    foreign_keys: tuple[ForeignKey, ...]
    values: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    _use_soft: bool = field(default=False, repr=False)

    # ---- lookup -------------------------------------------------------------
    def table(self, name: str) -> Table:
        if name in self.tables:
            return self.tables[name]
        for key, table in self.tables.items():
            if key.lower() == name.lower():
                return table
        raise KeyError(name)

    def column(self, key: str) -> Column:
        table, _, column = key.partition(".")
        return self.table(table).column(column)

    @cached_property
    def columns(self) -> tuple[Column, ...]:
        return tuple(c for t in self.tables.values() for c in t.columns)

    def edges(self, table: str) -> list[ForeignKey]:
        edges = [fk for fk in self.foreign_keys if table in (fk.table, fk.ref_table)
                 and fk.table != fk.ref_table]
        if self._use_soft:
            edges += [fk for fk in self.soft_edges if table in (fk.table, fk.ref_table)]
        return edges

    @cached_property
    def soft_edges(self) -> tuple[ForeignKey, ...]:
        """Undeclared links between same-named, same-kind columns of otherwise
        unconnected tables (used only when no declared path exists)."""
        edges = []
        names = list(self.tables)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if any(fk.other(a) == b for fk in self.foreign_keys if a in (fk.table, fk.ref_table)):
                    continue
                for col in self.tables[a].columns:
                    if col.name.lower() in ("name", "id", "description", "details", "type"):
                        continue
                    try:
                        other = self.tables[b].column(col.name)
                    except KeyError:
                        continue
                    if other.kind == col.kind:
                        edges.append(ForeignKey(a, (col.name,), b, (other.name,), declared=False))
                        break
        return tuple(edges)

    def neighbors(self, table: str) -> list[str]:
        return sorted({fk.other(table) for fk in self.edges(table)})

    def is_bridge(self, table: str) -> bool:
        """An association table: two or more outgoing FKs and few own attributes."""
        outgoing = [fk for fk in self.foreign_keys if fk.table == table and fk.ref_table != table]
        if len(outgoing) < 2:
            return False
        fk_columns = {c for fk in outgoing for c in fk.columns}
        own = [c for c in self.tables[table].columns if c.name not in fk_columns
               and not c.primary_key]
        return len(own) <= 2

    # ---- join paths ---------------------------------------------------------
    def shortest_paths(self, start: str, goal: str, limit: int = 4) -> list[list[JoinStep]]:
        """All shortest FK paths from ``start`` to ``goal`` (bounded)."""
        if start == goal:
            return [[]]
        best: list[list[JoinStep]] = []
        queue: deque[tuple[str, list[JoinStep]]] = deque([(start, [])])
        depth: dict[str, int] = {start: 0}
        while queue:
            node, path = queue.popleft()
            if best and len(path) >= len(best[0]):
                continue
            for fk in self.edges(node):
                nxt = fk.other(node)
                if any(step.table == nxt for step in path) or nxt == start:
                    continue
                if depth.get(nxt, 10 ** 6) < len(path) + 1 and nxt != goal:
                    continue
                depth[nxt] = len(path) + 1
                new_path = path + [JoinStep(nxt, node, fk)]
                if nxt == goal:
                    if not best or len(new_path) == len(best[0]):
                        best.append(new_path)
                    if len(best) >= limit:
                        return best
                else:
                    queue.append((nxt, new_path))
        return best

    def join_tree(self, tables: Iterable[str], root: str | None = None) -> list[JoinStep] | None:
        """Greedy Steiner tree connecting ``tables``; None when disconnected.

        Declared/inferred foreign keys are used first; same-named columns serve
        as a fallback only when the declared graph cannot connect the tables.
        """
        tables = list(tables)
        tree = self._join_tree(tables, root)
        if tree is None and self.soft_edges:
            self._use_soft = True
            try:
                tree = self._join_tree(tables, root)
            finally:
                self._use_soft = False
        return tree

    def _join_tree(self, tables: Iterable[str], root: str | None = None) -> list[JoinStep] | None:
        wanted = list(dict.fromkeys(tables))
        if not wanted:
            return []
        root = root or wanted[0]
        in_tree = {root}
        steps: list[JoinStep] = []
        remaining = [t for t in wanted if t != root]
        while remaining:
            best_path: list[JoinStep] | None = None
            best_target = None
            for target in remaining:
                for source in sorted(in_tree):
                    for path in self.shortest_paths(source, target, limit=1):
                        if best_path is None or len(path) < len(best_path):
                            best_path, best_target = path, target
            if best_path is None:
                return None
            for step in best_path:
                if step.table not in in_tree:
                    steps.append(step)
                    in_tree.add(step.table)
            remaining = [t for t in remaining if t not in in_tree]
            assert best_target is not None
        return steps

    @cached_property
    def _parents(self) -> dict[str, list[str]]:
        parents: dict[str, list[str]] = {}
        for fk in self.foreign_keys:
            if fk.table == fk.ref_table:
                continue
            for column, ref in zip(fk.columns, fk.ref_columns):
                parents.setdefault(f"{fk.table}.{column}", []).append(f"{fk.ref_table}.{ref}")
        return parents

    @cached_property
    def _children(self) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {}
        for child, parents in self._parents.items():
            for parent in parents:
                children.setdefault(parent, []).append(child)
        return children

    def equivalents(self, key: str) -> tuple[str, ...]:
        """Columns holding the same value *for joined rows*: FK ancestors and descendants.

        Siblings are excluded: student.dept_name and course.dept_name both reference
        department.dept_name, but a student's department is not a course's department.
        """
        out = [key]
        stack = [key]
        while stack:                      # ancestors
            for parent in self._parents.get(stack.pop(), ()):
                if parent not in out:
                    out.append(parent)
                    stack.append(parent)
        stack = [key]
        while stack:                      # descendants
            for child in self._children.get(stack.pop(), ()):
                if child not in out:
                    out.append(child)
                    stack.append(child)
        return tuple(out)

    def canonical(self, key: str) -> str:
        """The topmost referenced ancestor of ``key`` (itself when not an FK column)."""
        current = key
        seen = {key}
        while self._parents.get(current):
            nxt = self._parents[current][0]
            if nxt in seen:
                break
            seen.add(nxt)
            current = nxt
        return current

    def parallel_edges(self, a: str, b: str) -> list[ForeignKey]:
        return [fk for fk in self.edges(a) if fk.other(a) == b]

    # ---- presentation -------------------------------------------------------
    def describe(self) -> dict:
        return {
            "path": self.path,
            "tables": [
                {"name": t.name, "label": t.label, "rows": t.row_count,
                 "primary_key": list(t.primary_key),
                 "columns": [{"name": c.name, "type": c.declared_type, "kind": c.kind,
                              "primary_key": c.primary_key, "label": c.label,
                              "distinct": c.distinct_count} for c in t.columns]}
                for t in self.tables.values()],
            "foreign_keys": [{"table": fk.table, "columns": list(fk.columns),
                              "ref_table": fk.ref_table, "ref_columns": list(fk.ref_columns),
                              "declared": fk.declared} for fk in self.foreign_keys],
        }


# ---- introspection --------------------------------------------------------------

def _declared_kind(declared: str) -> str | None:
    d = declared.upper()
    if not d:
        return None
    if "BOOL" in d:
        return BOOLEAN
    if "DATE" in d or "TIME" in d or d == "YEAR":
        return DATE
    if any(k in d for k in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC", "BIT")):
        return NUMBER
    if any(k in d for k in ("CHAR", "TEXT", "CLOB", "STRING")):
        return TEXT
    return OTHER


def _profile_kind(declared_kind: str | None, sample: list[object]) -> str:
    values = [v for v in sample if v is not None and v != ""]
    if not values:
        return declared_kind or TEXT
    numeric = sum(isinstance(v, (int, float)) or (isinstance(v, str) and bool(_NUMERIC.match(v)))
                  for v in values)
    dates = sum(isinstance(v, str) and bool(_ISO_DATE.match(v.strip())) for v in values)
    if declared_kind == DATE:
        return DATE
    if dates >= 0.9 * len(values):
        return DATE
    if declared_kind == BOOLEAN:
        return BOOLEAN
    if numeric >= 0.95 * len(values):
        return NUMBER
    if declared_kind == NUMBER and numeric >= 0.5 * len(values):
        return NUMBER
    return TEXT if declared_kind in (None, TEXT, OTHER, NUMBER) else declared_kind


def _storage(sample: list[object]) -> str:
    counts = {"integer": 0, "real": 0, "text": 0, "blob": 0}
    for value in sample:
        if isinstance(value, bool) or isinstance(value, int):
            counts["integer"] += 1
        elif isinstance(value, float):
            counts["real"] += 1
        elif isinstance(value, bytes):
            counts["blob"] += 1
        else:
            counts["text"] += 1
    if not sample:
        return "text"
    if counts["real"] and counts["real"] + counts["integer"] >= counts["text"]:
        return "real"
    return max(counts, key=lambda k: counts[k])


def _infer_foreign_keys(tables: dict[str, Table], declared: list[ForeignKey]) -> list[ForeignKey]:
    """Infer obvious undeclared links: ``<table>_id`` columns matching a single-column PK."""
    known = {(fk.table, fk.columns) for fk in declared}
    inferred = []
    pk_index = {}
    for table in tables.values():
        if len(table.primary_key) == 1:
            pk_index.setdefault(table.primary_key[0].lower(), []).append(table.name)
    for table in tables.values():
        for column in table.columns:
            if (table.name, (column.name,)) in known or column.primary_key and len(table.primary_key) == 1:
                continue
            targets = [t for t in pk_index.get(column.name.lower(), []) if t != table.name]
            if len(targets) == 1 and column.name.lower().endswith("id"):
                target = tables[targets[0]]
                inferred.append(ForeignKey(table.name, (column.name,), target.name,
                                           (target.primary_key[0],), declared=False))
    return inferred


def introspect(conn: sqlite3.Connection, path: str = "") -> Schema:
    table_names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY rowid")]
    tables: dict[str, Table] = {}
    raw_fks: list[ForeignKey] = []
    values: dict[str, tuple[str, ...]] = {}
    for name in table_names:
        qname = quote_identifier(name)
        info = conn.execute(f"PRAGMA table_info({qname})").fetchall()
        try:
            row_count = conn.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]
        except sqlite3.Error:
            row_count = 0
        pk = tuple(r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5]))
        columns = []
        for _, col_name, declared, not_null, _, pk_pos in info:
            qcol = quote_identifier(col_name)
            try:
                sample = [r[0] for r in conn.execute(
                    f"SELECT {qcol} FROM {qname} WHERE {qcol} IS NOT NULL LIMIT {SAMPLE_SIZE}")]
                distinct, nulls = conn.execute(
                    f"SELECT COUNT(DISTINCT {qcol}), SUM({qcol} IS NULL) FROM {qname}").fetchone()
            except sqlite3.Error:
                sample, distinct, nulls = [], None, None
            kind = _profile_kind(_declared_kind(declared or ""), sample)
            columns.append(Column(name, col_name, declared or "", kind, bool(pk_pos),
                                  bool(not_null), humanize(col_name), distinct, nulls or 0,
                                  _storage(sample)))
            if kind in (TEXT, DATE, BOOLEAN, OTHER) and distinct and distinct <= VALUE_INDEX_LIMIT:
                values[f"{name}.{col_name}"] = tuple(
                    str(r[0]) for r in conn.execute(
                        f"SELECT DISTINCT {qcol} FROM {qname} WHERE {qcol} IS NOT NULL "
                        f"LIMIT {VALUE_INDEX_LIMIT}"))
        tables[name] = Table(name, humanize(name), tuple(columns), pk, row_count)
        for row in conn.execute(f"PRAGMA foreign_key_list({qname})").fetchall():
            fk_id, _, ref_table, from_col, to_col = row[0], row[1], row[2], row[3], row[4]
            raw_fks.append(ForeignKey(name, (from_col,), ref_table, (to_col,)))
    fks = _normalize_fks(tables, raw_fks)
    fks += _infer_foreign_keys(tables, fks)
    return Schema(path, tables, tuple(fks), values)


def _normalize_fks(tables: dict[str, Table], raw: list[ForeignKey]) -> list[ForeignKey]:
    """Resolve case/missing targets; drop references to nonexistent tables or columns."""
    by_lower = {name.lower(): name for name in tables}
    result: list[ForeignKey] = []
    seen = set()
    for fk in raw:
        ref = by_lower.get(fk.ref_table.lower())
        if ref is None:
            continue
        ref_table = tables[ref]
        ref_cols = []
        for column, ref_col in zip(fk.columns, fk.ref_columns):
            if ref_col is None:
                ref_col = ref_table.primary_key[0] if len(ref_table.primary_key) == 1 else column
            try:
                ref_cols.append(ref_table.column(ref_col).name)
            except KeyError:
                ref_cols = []
                break
        try:
            cols = [tables[fk.table].column(c).name for c in fk.columns]
        except KeyError:
            continue
        if not ref_cols:
            continue
        normalized = ForeignKey(fk.table, tuple(cols), ref, tuple(ref_cols))
        if normalized.key not in seen:
            seen.add(normalized.key)
            result.append(normalized)
    return result


_CACHE: dict[tuple[str, float, int], Schema] = {}
_CACHE_LOCK = threading.Lock()


def load_schema(db_path: str | Path) -> Schema:
    """Introspect (and cache by path + mtime + size) a SQLite database read-only."""
    path = Path(db_path).resolve()
    stat = path.stat()
    key = (str(path), stat.st_mtime, stat.st_size)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    try:
        schema = introspect(conn, str(path))
    finally:
        conn.close()
    with _CACHE_LOCK:
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = schema
    return schema
