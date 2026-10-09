"""Deterministic schema grounding for an arbitrary SQLite database.

Everything the interpreter may reference -- tables, columns, types, keys,
relationships and join paths -- comes from here.  Jev never names an
identifier; it only selects among objects built by this module.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path

from intentsql.text import humanize, lemma

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
    blank_count: int = 0      # empty strings, which people also call 'missing'
    formula: tuple[str, str, str] | None = None   # derived measure: (operator, left key, right key)

    @property
    def key(self) -> str:
        return f"{self.table}.{self.name}"

    @property
    def is_numeric(self) -> bool:
        return self.kind == NUMBER

    @property
    def is_identifier(self) -> bool:
        return self.formula is None and (self.primary_key or bool(_IDENTIFIER.search(self.name)))


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
    role_of: str | None = None        # a role instance of this physical table (see _role_tables)

    @property
    def physical(self) -> str:
        return self.role_of or self.name

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

    def with_columns(self, extra: Iterable[Column]) -> Schema:
        """A request-scoped copy with derived columns added (the cached schema is shared)."""
        tables = dict(self.tables)
        for column in extra:
            table = tables[column.table]
            tables[column.table] = replace(table, columns=table.columns + (column,))
        return Schema(self.path, tables, self.foreign_keys, self.values)

    def requires(self, key: str) -> tuple[str, ...]:
        """Tables a column needs in scope: its own, plus the operands' for a derived measure."""
        column = self.column(key)
        if column.formula is None:
            return (column.table,)
        _, left, right = column.formula
        return tuple(dict.fromkeys((column.table,) + self.requires(left) + self.requires(right)))

    @cached_property
    def columns(self) -> tuple[Column, ...]:
        return tuple(c for t in self.tables.values() for c in t.columns)

    def edges(self, table: str, soft: bool = False) -> list[ForeignKey]:
        edges = [fk for fk in self.foreign_keys if table in (fk.table, fk.ref_table)
                 and fk.table != fk.ref_table]
        if soft:
            edges += [fk for fk in self.soft_edges if table in (fk.table, fk.ref_table)]
        return edges

    @cached_property
    def soft_edges(self) -> tuple[ForeignKey, ...]:
        """Undeclared links between same-named, same-kind columns of otherwise
        unconnected tables (used only when no declared path exists)."""
        edges = []
        names = [n for n in self.tables if self.tables[n].role_of is None]
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

    def is_role(self, table: str) -> bool:
        return self.tables[table].role_of is not None

    def countable(self, table: str) -> bool:
        """Tables whose records a request can count or list as such (not bridges or roles)."""
        return not self.is_bridge(table) and not self.is_role(table)

    def neighbors(self, table: str) -> list[str]:
        return sorted({fk.other(table) for fk in self.edges(table)})

    def is_bridge(self, table: str) -> bool:
        """An association table (singer_in_concert, has_amenity): two or more outgoing
        FKs and no identity of its own -- its key is absent or made of its foreign keys,
        or it is a bare surrogate id with nothing else to describe.  Tables such as
        orders(id, customer_id, store_id, date, status) are entities, not bridges."""
        outgoing = [fk for fk in self.foreign_keys if fk.table == table and fk.ref_table != table]
        if len(outgoing) < 2:
            return False
        t = self.tables[table]
        fk_columns = {c for fk in outgoing for c in fk.columns}
        own = [c for c in t.columns if c.name not in fk_columns and not c.primary_key]
        if not t.primary_key or set(t.primary_key) <= fk_columns:
            return len(own) <= 2
        return not own

    # ---- join paths ---------------------------------------------------------
    def shortest_paths(self, start: str, goal: str, limit: int = 4,
                       soft: bool = False) -> list[list[JoinStep]]:
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
            for fk in self.edges(node, soft):
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
            tree = self._join_tree(tables, root, soft=True)
        return tree

    def _join_tree(self, tables: Iterable[str], root: str | None = None,
                   soft: bool = False) -> list[JoinStep] | None:
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
                    for path in self.shortest_paths(source, target, limit=1, soft=soft):
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

    @cached_property
    def _key_columns(self) -> frozenset[str]:
        keys = {c.key for c in self.columns if c.is_identifier}
        for fk in self.foreign_keys:
            keys.update(f"{fk.table}.{c}" for c in fk.columns)
            keys.update(f"{fk.ref_table}.{c}" for c in fk.ref_columns)
        return frozenset(keys)

    def is_key(self, key: str) -> bool:
        """Identifier-like: a primary key, any column of a foreign key, or an *_id name."""
        return key in self._key_columns

    def one_to_many(self, a: str, b: str) -> bool:
        """True when following the shortest path from ``a`` to ``b`` can reach several
        ``b`` rows for one ``a`` row (some step goes from a referenced table to a
        table that references it)."""
        if a == b:
            return False
        paths = self.shortest_paths(a, b, limit=1)
        if not paths:
            return False
        return any(step.fk.table == step.table and step.fk.ref_table == step.from_table
                   for step in paths[0])

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
                for t in self.tables.values() if t.role_of is None],
            "foreign_keys": [{"table": fk.table, "columns": list(fk.columns),
                              "ref_table": self.tables[fk.ref_table].physical, "ref_columns": list(fk.ref_columns),
                              "declared": fk.declared} for fk in self.foreign_keys
                             if self.tables[fk.table].role_of is None],
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
            name = column.name.lower()
            if not targets and name.endswith("id") and not column.primary_key:
                # Role-named references: end_station_id -> station(id), manager_id -> manager.
                words = set(humanize(column.name).split()) - {"id"}
                targets = [t.name for t in tables.values()
                           if t.name != table.name and len(t.primary_key) == 1
                           and t.primary_key[0].lower() in ("id", f"{t.name.lower()}_id")
                           and lemma_words(t.label) <= {lemma(w) for w in words}]
            if (not targets and name.endswith("id") and len(table.primary_key) == 1
                    and set(humanize(column.name).split()) & SELF_ROLES):
                targets = [table.name]          # employees.manager_id -> employees.id
            if len(targets) == 1 and name.endswith("id"):
                target = tables[targets[0]]
                pk = target.column(target.primary_key[0])
                if (pk.kind == column.kind or {pk.kind, column.kind} <= {NUMBER, TEXT}) and (
                        table.name, (column.name,)) not in known:
                    inferred.append(ForeignKey(table.name, (column.name,), target.name,
                                               (target.primary_key[0],), declared=False))
    return inferred


SELF_ROLES = {"manager", "boss", "supervisor", "parent", "reports", "mentor", "superior", "head",
              "leader", "referrer", "referred", "spouse", "partner", "predecessor", "successor"}


def flag_values(values: tuple[str, ...]) -> tuple[str, str] | None:
    """(true value, false value) when a column stores a two-valued flag."""
    pairs = (("1", "0"), ("T", "F"), ("Y", "N"), ("yes", "no"), ("true", "false"), ("Yes", "No"),
             ("TRUE", "FALSE"), ("y", "n"), ("t", "f"))
    present = set(values)
    for yes, no in pairs:
        if present == {yes, no}:
            return yes, no
    return None


def lemma_words(label: str) -> set[str]:
    return {lemma(w) for w in label.split()}


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
                distinct, nulls, blanks = conn.execute(
                    f"SELECT COUNT(DISTINCT {qcol}), SUM({qcol} IS NULL), "
                    f"SUM(TRIM(CAST({qcol} AS TEXT)) = '') FROM {qname}").fetchone()
            except sqlite3.Error:
                sample, distinct, nulls, blanks = [], None, None, 0
            kind = _profile_kind(_declared_kind(declared or ""), sample)
            columns.append(Column(name, col_name, declared or "", kind, bool(pk_pos),
                                  bool(not_null), humanize(col_name), distinct, nulls or 0,
                                  _storage(sample), blanks or 0))
            if (kind in (TEXT, DATE, BOOLEAN, OTHER) and distinct and distinct <= VALUE_INDEX_LIMIT) or (
                    kind == NUMBER and distinct == 2):
                values[f"{name}.{col_name}"] = tuple(
                    str(r[0]) for r in conn.execute(
                        f"SELECT DISTINCT {qcol} FROM {qname} WHERE {qcol} IS NOT NULL "
                        f"LIMIT {VALUE_INDEX_LIMIT}"))
        tables[name] = Table(name, humanize(name), tuple(columns), pk, row_count)
        # One FK per constraint id: a composite key (block_floor, block_code) is one
        # relationship, joined on both columns, not two unrelated single-column links.
        grouped: dict[int, list[tuple]] = {}
        for row in conn.execute(f"PRAGMA foreign_key_list({qname})").fetchall():
            grouped.setdefault(row[0], []).append(row)
        for rows in grouped.values():
            rows.sort(key=lambda r: r[1])
            ref_table = rows[0][2]
            raw_fks.append(ForeignKey(name, tuple(r[3] for r in rows), ref_table,
                                      tuple(r[4] for r in rows)))
    fks = _normalize_fks(tables, raw_fks)
    fks += _infer_foreign_keys(tables, fks)
    tables, fks = _role_tables(tables, fks, values, parallel=PARALLEL_ROLES)
    return Schema(path, tables, tuple(fks), values)


def _role_label(schema_tables: dict[str, Table], fk: ForeignKey) -> str:
    base = schema_tables[fk.ref_table]
    words = [w for w in humanize(" ".join(fk.columns)).split() if w not in ("id", "code", "no", "num")]
    entity = [lemma(w) for w in base.label.split()]
    if not words:
        return base.label
    if entity[-1] not in {lemma(w) for w in words}:
        words += base.label.split()
    return " ".join(words)


def _role_tables(tables: dict[str, Table], fks: list[ForeignKey], values: dict[str, tuple[str, ...]],
                 parallel: bool = False) -> tuple[dict[str, Table], list[ForeignKey]]:
    """Role instances for tables referenced in several roles.

    flights(source_airport, dest_airport) both reference airports; employees.manager_id
    references employees.  A role (the destination airport, the manager) is a second
    instance of the referenced table, joined through its own foreign key, so a request
    can use two roles at once ('flights from Aberdeen to Ashley', 'employees earning more
    than their manager').  The first of several parallel references stays a direct link
    to the table; each further one and every self-reference becomes a role table with
    the referenced table's columns, values and outgoing relationships.
    """
    groups: dict[tuple[str, str, tuple[str, ...]], list[ForeignKey]] = {}
    for fk in fks:
        groups.setdefault((fk.table, fk.ref_table, fk.ref_columns), []).append(fk)
    roles: list[ForeignKey] = []
    for (table, ref, _), members in groups.items():
        if table == ref:
            roles += members
        elif parallel and len(members) >= 2:
            roles += members[1:]
    if not roles:
        return tables, fks
    out_tables = dict(tables)
    out_fks = [fk for fk in fks if fk not in roles]
    for fk in roles:
        base = tables[fk.ref_table]
        name = f"{fk.table}__{'_'.join(fk.columns)}"
        while name in out_tables:
            name += "_"
        columns = tuple(replace(c, table=name) for c in base.columns)
        label = _role_label(tables, fk)
        if any(t.label == label for t in out_tables.values()):
            label = f"{humanize(' '.join(fk.columns))} {base.label}"
        out_tables[name] = Table(name, label, columns, base.primary_key,
                                 base.row_count, role_of=base.name)
        out_fks.append(ForeignKey(fk.table, fk.columns, name, fk.ref_columns, fk.declared))
        for other in fks:
            if other.table == base.name and other.ref_table != base.name and other not in roles:
                out_fks.append(ForeignKey(name, other.columns, other.ref_table, other.ref_columns,
                                          other.declared))
        for column in base.columns:
            if column.key in values:
                values[f"{name}.{column.name}"] = values[column.key]
    return out_tables, out_fks


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
        for position, (column, ref_col) in enumerate(zip(fk.columns, fk.ref_columns)):
            if ref_col is None:
                pk = ref_table.primary_key
                ref_col = pk[position] if len(pk) == len(fk.columns) else column
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


# Parallel references (source/destination airport) are disambiguated per query by a
# relationship choice; role tables for them are an experiment (docs/EXPERIMENTS.md).
PARALLEL_ROLES = os.environ.get("INTENTSQL_PARALLEL_ROLES", "0") == "1"
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
