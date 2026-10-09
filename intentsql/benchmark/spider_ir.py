"""Convert Spider's parsed gold SQL into IntentSQL IR (compiler coverage testing).

This is test tooling: it proves that the IR and compiler can express the
reference queries.  It never participates in answering a question.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from intentsql import ir
from intentsql.benchmark.spider import SPLIT_FILES, spider_root

AGG = (None, "MAX", "MIN", "COUNT", "SUM", "AVG")
UNIT = (None, "-", "+", "*", "/")
OPS = ("not", "between", "=", ">", "<", ">=", "<=", "!=", "in", "like", "is", "exists")


class Unconvertible(ValueError):
    pass


@lru_cache(maxsize=4)
def _tables(split: str) -> dict[str, dict]:
    path = spider_root() / SPLIT_FILES[split][2]
    return {db["db_id"]: db for db in json.loads(Path(path).read_text())}


class _Converter:
    def __init__(self, db: dict):
        self.db = db
        self.counter = 0
        self.allow_distinct = False

    def column(self, col_id: int, aliases: dict[int, str]) -> ir.Expr:
        if col_id == 0:
            return ir.Star()
        table_id, name = self.db["column_names_original"][col_id]
        if table_id not in aliases:
            raise Unconvertible(f"column {name} from table outside FROM")
        return ir.Col(aliases[table_id], name)

    def col_unit(self, unit: Any, aliases: dict[int, str]) -> ir.Expr:
        agg_id, col_id, distinct = unit
        expr = self.column(col_id, aliases)
        if AGG[agg_id]:
            return ir.Agg(AGG[agg_id], expr, bool(distinct))
        if distinct and not self.allow_distinct:
            raise Unconvertible("distinct column outside aggregate")
        return expr

    def val_unit(self, unit: Any, aliases: dict[int, str]) -> ir.Expr:
        op, left, right = unit
        expr = self.col_unit(left, aliases)
        if op:
            expr = ir.BinOp(UNIT[op], expr, self.col_unit(right, aliases))
        return expr

    def value(self, value: Any, aliases: dict[int, str]) -> ir.Expr:
        if isinstance(value, dict):
            return ir.ScalarQuery(self.query(value, aliases))
        if isinstance(value, list):  # column reference (col_unit)
            return self.col_unit(value, aliases)
        if isinstance(value, str):
            text = value
            if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
                text = text[1:-1]
            return ir.Lit(text)
        if isinstance(value, float) and value.is_integer():
            return ir.Lit(int(value))
        return ir.Lit(value)

    def conds(self, items: list, aliases: dict[int, str]) -> ir.Cond | None:
        if not items:
            return None
        groups: list[list[ir.Cond]] = [[]]
        for item in items:
            if item == "or":
                groups.append([])
            elif item == "and":
                continue
            else:
                groups[-1].append(self.cond(item, aliases))
        ands = [g[0] if len(g) == 1 else ir.And(tuple(g)) for g in groups]
        return ands[0] if len(ands) == 1 else ir.Or(tuple(ands))

    def cond(self, unit: Any, aliases: dict[int, str]) -> ir.Cond:
        negated, op_id, val_unit, v1, v2 = unit
        op = OPS[op_id]
        left = self.val_unit(val_unit, aliases)
        if op == "between":
            return ir.Between(left, self.value(v1, aliases), self.value(v2, aliases), negated)
        if op == "in":
            if not isinstance(v1, dict):
                raise Unconvertible("IN list")
            return ir.InQuery(left, self.query(v1, aliases), negated)
        if op == "like":
            pattern = self.value(v1, aliases)
            if not isinstance(pattern, ir.Lit):
                raise Unconvertible("LIKE non-literal")
            return ir.Like(left, str(pattern.value), negated)
        if op == "is":
            return ir.IsNull(left, negated)
        if op == "exists":
            return ir.Exists(self.query(v1, aliases), negated)
        if op == "not":
            raise Unconvertible("bare NOT")
        cond: ir.Cond = ir.Compare(left, op, self.value(v1, aliases))
        return ir.Not(cond) if negated else cond

    def query(self, sql: dict, outer: dict[int, str] | None = None) -> ir.Query:
        for key, op in (("intersect", "INTERSECT"), ("union", "UNION"), ("except", "EXCEPT")):
            if sql.get(key):
                base = dict(sql, intersect=None, union=None, **{"except": None})
                return ir.SetOp(op, self.query(base, outer), self.query(sql[key], outer))
        units = sql["from"]["table_units"]
        aliases: dict[int, str] = dict(outer or {})
        refs: list[ir.TableRef] = []
        local_ids: set[int] = set()
        for kind, ref in units:
            if kind != "table_unit":
                raise Unconvertible("derived table")
            if ref in local_ids:
                raise Unconvertible("self join")
            local_ids.add(ref)
            self.counter += 1
            alias = f"{self.db['table_names_original'][ref]}#{self.counter}"
            aliases[ref] = alias
            refs.append(ir.TableRef(self.db["table_names_original"][ref], alias))
        joins = []
        on_conds = [c for c in sql["from"]["conds"] if c not in ("and", "or")]
        pending = list(on_conds)
        for ref in refs[1:]:
            on = []
            for cond in list(pending):
                _, op_id, val_unit, v1, _ = cond
                if OPS[op_id] != "=" or not isinstance(v1, list):
                    continue
                left = self.val_unit(val_unit, aliases)
                right = self.col_unit(v1, aliases)
                if isinstance(left, ir.Col) and isinstance(right, ir.Col) and ref.alias in (
                        left.source, right.source):
                    other = right if left.source == ref.alias else left
                    mine = left if left.source == ref.alias else right
                    if other.source in [r.alias for r in refs[:refs.index(ref)]]:
                        on.append((other, mine))
                        pending.remove(cond)
            if not on:
                raise Unconvertible("cross join")
            joins.append(ir.Join(ref, tuple(on)))
        where = self.conds(sql["where"], aliases)
        if pending:
            extra = [self.cond(c, aliases) for c in pending]
            where = ir.conjoin(extra + ([where] if where is not None else []))
        distinct, select = sql["select"]
        items = []
        for agg_id, val_unit in select:
            self.allow_distinct = bool(AGG[agg_id])
            try:
                expr = self.val_unit(val_unit, aliases)
            finally:
                self.allow_distinct = False
            if AGG[agg_id]:
                if isinstance(expr, ir.Agg):
                    raise Unconvertible("nested aggregate")
                inner_distinct = False
                if isinstance(expr, ir.Col | ir.Star) and val_unit[1][2]:
                    inner_distinct = True
                expr = ir.Agg(AGG[agg_id], expr, inner_distinct)
            items.append(ir.SelectItem(expr))
        order = ()
        if sql["orderBy"]:
            direction, units_ = sql["orderBy"]
            order = tuple(ir.OrderItem(self.val_unit(u, aliases), direction == "desc")
                          for u in units_)
        return ir.Select(
            items=tuple(items), from_=ir.From(refs[0], tuple(joins)), where=where,
            group_by=tuple(self.col_unit(u, aliases) for u in sql["groupBy"]),
            having=self.conds(sql["having"], aliases), order_by=order,
            limit=sql["limit"], distinct=bool(distinct))


def to_ir(split: str, db_id: str, sql: dict) -> ir.Query:
    return _Converter(_tables(split)[db_id]).query(sql)
