"""Example questions for a database, generated from its schema.

A first-time user sees questions that this database can actually answer:
counts, rankings, per-group counts, anti-joins and filters on stored values.
Everything comes from the inspected schema; nothing is invented.
"""

from __future__ import annotations

import re

from intentsql.intent import entity
from intentsql.render import plural
from intentsql.schema import NUMBER, TEXT, Column, Schema, flag_values

_NAME = re.compile(r"\b(name|title)\b")


def _name_column(schema: Schema, table: str) -> Column | None:
    for column in schema.tables[table].columns:
        if column.kind == TEXT and _NAME.search(column.label):
            return column
    return None


_CODE = re.compile(r"\b(zip|code|phone|year|season|episode|number|no|num|id|postal|fax|lat|long|"
                   r"latitude|longitude|rank|order|position)\b")
_MEASURE = re.compile(r"\b(price|salary|amount|cost|total|weight|height|age|score|budget|population|"
                      r"expenditure|revenue|sales|rating|duration|length|capacity|balance|points)\b")


def _measures(schema: Schema, table: str) -> list[Column]:
    found = [c for c in schema.tables[table].columns
             if c.kind == NUMBER and not c.is_identifier and not schema.is_key(c.key)
             and (c.distinct_count or 0) > 2 and not _CODE.search(c.label)]
    return sorted(found, key=lambda c: not _MEASURE.search(c.label))


def examples(schema: Schema, limit: int = 6) -> list[str]:
    tables = [t for t in schema.tables.values() if schema.countable(t.name) and t.row_count > 0]
    tables.sort(key=lambda t: -t.row_count)
    out: list[str] = []

    def add(text: str) -> None:
        if text not in out:
            out.append(text)

    if tables:
        add(f"How many {plural(entity(schema, tables[0].name))} are there?")
    for table in tables:
        name, measures = _name_column(schema, table.name), _measures(schema, table.name)
        if name and measures:
            add(f"Which {entity(schema, table.name)} has the highest {measures[0].label}?")
            break
    for fk in schema.foreign_keys:
        child, parent = fk.table, fk.ref_table
        if (child != parent and schema.countable(child) and schema.countable(parent)
                and _name_column(schema, parent) and schema.tables[child].row_count > 0):
            add(f"How many {plural(entity(schema, child))} does each {entity(schema, parent)} have?")
            add(f"Which {plural(entity(schema, parent))} have no {plural(entity(schema, child))}?")
            break
    for table in tables:
        for column in table.columns:
            values = schema.values.get(column.key, ())
            if column.kind != TEXT or column.is_identifier or not 2 <= len(values) <= 12:
                continue
            if flag_values(values):
                continue
            value = next((v for v in values if 3 <= len(v.strip()) <= 30), None)
            if value:
                add(f"How many {plural(entity(schema, table.name))} have {column.label} {value}?")
                break
        else:
            continue
        break
    for table in tables:
        measures = _measures(schema, table.name)
        groups = [c for c in table.columns if c.kind == TEXT and not c.is_identifier
                  and 2 <= len(schema.values.get(c.key, ())) <= 20]
        if measures and groups:
            add(f"What is the average {measures[0].label} for each {groups[0].label}?")
            break
    return out[:limit]
