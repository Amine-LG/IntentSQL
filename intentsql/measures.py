"""Derived measures: arithmetic over stored columns that a request may refer to.

"Revenue" is quantity times unit price, "duration" is end date minus start
date, "lower support than oppose rate" compares two columns.  None of these
are stored, so they are not in the decision space unless code proposes them.
This module proposes a small number of derived columns, each grounded in the
request (cue words) and in the schema (column names and types), and adds them
to a request-scoped copy of the schema.  Jev then chooses among them exactly
as among stored columns; it never writes an expression.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from intentsql.schema import DATE, NUMBER, TEXT, Column, Schema

MAX_MEASURES = 6

DIFF_CUE = re.compile(r"\b(difference|differ\w*|minus|subtract\w*|gap|margin|profit|net|change|"
                      r"increase\w*|decrease\w*|growth|remaining|left over)\b", re.I)
DURATION_CUE = re.compile(r"\b(duration|how long|longest|shortest|lasted|last\w*|took|elapsed|span|"
                          r"length of time|days? between|time between|period|stay\w*)\b", re.I)
SUM_CUE = re.compile(r"\b(plus|combined|together|sum of .+ and|total of .+ and|added to|"
                     r"total \w+ and \w+)\b", re.I)
RATIO_CUE = re.compile(r"\b(ratio|divided by|proportion)\b", re.I)
PRODUCT_CUE = re.compile(r"\b(revenue|sales|turnover|income|worth|spent|spend\w*|"
                         r"(?:total|order|line|item|stock|inventory) (?:value|price|cost|amount)|"
                         r"value of|times|multiplied)\b", re.I)
QUANTITY = re.compile(r"\b(quantity|qty|units?|amount ordered|number of (?:items|units)|"
                      r"ordered amount|count)\b", re.I)
PRICE = re.compile(r"\b(price|cost|rate|fee|charge)\b", re.I)
START = re.compile(r"\b(start|from|begin\w*|open\w*|join\w*|hire\w*|arriv\w*|check in|admi\w*|"
                   r"order\w*|placed|created|borrow\w*)\b", re.I)
END = re.compile(r"\b(end|to|finish\w*|clos\w*|leav\w*|left|depart\w*|check out|discharg\w*|"
                 r"ship\w*|deliver\w*|return\w*|due|until)\b", re.I)
PART_KINDS = {"year": TEXT, "month": TEXT, "weekday": TEXT, "day": DATE}
PART_COUNTS = {"year": 20, "month": 24, "weekday": 7, "day": None}
SYMBOL = {"-": "minus", "+": "plus", "*": "×", "/": "divided by"}
PERIOD_CUE = re.compile(r"\b(year|month|week ?day|day of (?:the )?week|yearly|monthly|annual\w*|"
                        r"per day|each day|daily)\b", re.I)
PERIODS = (("year", re.compile(r"\b(year|yearly|annual\w*)\b", re.I)),
           ("month", re.compile(r"\b(month|monthly)\b", re.I)),
           ("weekday", re.compile(r"\b(week ?day|day of (?:the )?week)\b", re.I)),
           ("day", re.compile(r"\b(per day|each day|daily|by day)\b", re.I)))


def derive(question: str, schema: Schema, column_link: dict[str, float],
           mention: dict[str, int]) -> list[Column]:
    """Derived columns the request plausibly refers to (possibly none)."""
    proposals: list[tuple[str, str, str, str]] = []      # (operator, left, right, label)

    named = [c for c in schema.columns if column_link.get(c.key, 0) >= 0.5
             and c.kind in (NUMBER, DATE) and not c.is_identifier and c.formula is None]
    named.sort(key=lambda c: mention.get(c.key, 10_000))
    for i, a in enumerate(named):
        for b in named[i + 1:]:
            if a.key == b.key or a.kind != b.kind or not _close(schema, a.table, b.table):
                continue
            ops = []
            if DIFF_CUE.search(question) or (a.kind == DATE and DURATION_CUE.search(question)):
                ops.append("-")
            if a.kind == NUMBER and SUM_CUE.search(question):
                ops.append("+")
            if a.kind == NUMBER and RATIO_CUE.search(question):
                ops.append("/")
            for op in ops:
                left, right = (a, b)
                if a.kind == DATE and START.search(a.label) and END.search(b.label):
                    left, right = b, a          # end minus start
                proposals.append((op, left.key, right.key, _label(schema, op, left, right)))

    if DURATION_CUE.search(question):
        for table in schema.tables.values():
            dates = [c for c in table.columns if c.kind == DATE and c.formula is None]
            starts = [c for c in dates if START.search(c.label) and not END.search(c.label)]
            ends = [c for c in dates if END.search(c.label) and not START.search(c.label)]
            if len(starts) == 1 and len(ends) == 1:
                end, start = ends[0], starts[0]
                proposals.append(("-", end.key, start.key,
                                  f"{_entity(schema, table.name)} duration in days "
                                  f"({_short(end)} minus {_short(start)})"))

    product = PRODUCT_CUE.search(question)
    if product:
        word = product.group(0).lower().split()[-1]
        word = {"spent": "spending", "spend": "spending", "times": "value", "multiplied": "value",
                "of": "value", "worth": "value"}.get(word, word)
        for q in schema.columns:
            if q.kind != NUMBER or q.is_identifier or q.formula or not QUANTITY.search(q.label):
                continue
            for p in _price_columns(schema, q.table):
                proposals.append(("*", q.key, p.key,
                                  f"{word} ({_short(q)} × {_full(schema, p, q.table)})"))

    if PERIOD_CUE.search(question):
        # 'orders per month', 'the year with the most sales': a period of a stored date,
        # unless the schema already stores that period as a column.
        stored = {w for c in schema.columns for w in c.label.split()}
        dates = [c for c in schema.columns if c.kind == DATE and c.formula is None]
        dates.sort(key=lambda c: -column_link.get(c.key, 0))
        for part, cue in PERIODS:
            if cue.search(question) and part not in stored:
                for date in dates[:3]:
                    proposals.append((part, date.key, date.key, f"{date.label} {part}"))

    columns: list[Column] = []
    seen: set[tuple[str, str, str]] = set()
    for op, left, right, label in proposals:
        if (op, left, right) in seen or len(columns) >= MAX_MEASURES:
            continue
        seen.add((op, left, right))
        host = _host(schema, left, right)
        if op in PART_KINDS:
            columns.append(Column(host, f"__m{len(columns) + 1}", "", PART_KINDS[op], False, False,
                                  label, PART_COUNTS[op], None, "text", 0, (op, left, right)))
            continue
        columns.append(Column(host, f"__m{len(columns) + 1}", "", NUMBER, False, False, label,
                              None, None, "real", 0, (op, left, right)))
    return columns


def _close(schema: Schema, a: str, b: str) -> bool:
    if a == b:
        return True
    paths = schema.shortest_paths(a, b, limit=1)
    return bool(paths) and len(paths[0]) <= 2


def _host(schema: Schema, left: str, right: str) -> str:
    """The table a measure belongs to: the 'many' side when operands are in two tables
    (an order line's value, not a product's), so joining it never repeats rows."""
    a, b = left.split(".")[0], right.split(".")[0]
    if a == b:
        return a
    return b if schema.one_to_many(a, b) else a


def _price_columns(schema: Schema, table: str) -> Iterable[Column]:
    """Price-like columns of ``table`` or of a table it references directly."""
    own = [c for c in schema.tables[table].columns
           if c.kind == NUMBER and not c.is_identifier and c.formula is None and PRICE.search(c.label)]
    if own:
        return own
    found = []
    for fk in schema.foreign_keys:
        if fk.table == table and fk.ref_table != table:
            found += [c for c in schema.tables[fk.ref_table].columns
                      if c.kind == NUMBER and not c.is_identifier and PRICE.search(c.label)]
    return found


def _entity(schema: Schema, table: str) -> str:
    from intentsql.intent import entity
    return entity(schema, table)


def _short(column: Column) -> str:
    return column.label


def _full(schema: Schema, column: Column, home: str) -> str:
    if column.table == home:
        return column.label
    from intentsql.intent import column_phrase
    return column_phrase(schema, column.key)


def _label(schema: Schema, op: str, left: Column, right: Column) -> str:
    from intentsql.intent import column_phrase
    a, b = column_phrase(schema, left.key), column_phrase(schema, right.key)
    if op == "-" and left.kind == DATE:
        return f"days between {b} and {a}"
    return f"{a} {SYMBOL[op]} {b}"


def extend(schema: Schema, measures: list[Column]) -> Schema:
    return schema.with_columns(measures) if measures else schema


__all__ = ["derive", "extend", "MAX_MEASURES"]
