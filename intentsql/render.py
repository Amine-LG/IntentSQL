"""Plain-English rendering of an Intent.

The rendering is shown to users ("what IntentSQL understood") and is what Jev
compares with the request during verification, so it is written the way a
person would phrase the request: "Count the orders whose status is
'cancelled'", "List the name of each region", "... that have no orders".
Measured on Spider train, natural phrasing raised the verification score of
correct readings from ~0.15 to ~0.9 while wrong readings stayed low
(docs/EXPERIMENTS.md, E11).
"""

from __future__ import annotations

from typing import Any

from intentsql.intent import Condition, Intent, Ordering, OutputItem, _effective_groups, _is_year, entity
from intentsql.schema import DATE, Schema, flag_values
from intentsql.text import lemma

FUNC = {"SUM": "total", "AVG": "average", "MIN": "minimum", "MAX": "maximum"}
OPS = {"=": "is", "!=": "is not", ">": "is greater than", "<": "is less than",
       ">=": "is at least", "<=": "is at most", "contains": "contains", "starts": "starts with",
       "ends": "ends with", "not_contains": "does not contain"}
COUNT_OPS = {"=": "exactly", ">": "more than", "<": "fewer than", ">=": "at least", "<=": "at most",
             "!=": "a number other than"}


def plural(noun: str) -> str:
    words = noun.split()
    last = words[-1] if words else noun
    if last.endswith(("ics", "ies", "series", "data", "staff", "people")) or (
            last.endswith("s") and lemma(last) != last):
        return noun                                     # already plural
    if last.endswith(("s", "x", "z", "ch", "sh")):
        last += "es"
    elif last.endswith("y") and len(last) > 1 and last[-2] not in "aeiou":
        last = last[:-1] + "ies"
    else:
        last += "s"
    return " ".join(words[:-1] + [last])


def _value(value: Any) -> str:
    return f"'{value}'" if isinstance(value, str) else str(value)


def _group_label(schema: Schema, key: str, subject: str | None) -> str:
    """A group named by its entity when it is keyed by an id ('department', not 'department id')."""
    if schema.is_key(key):
        for member in schema.equivalents(key):
            column = schema.column(member)
            if column.primary_key and len(schema.table(column.table).primary_key) == 1:
                return entity(schema, column.table)
    return _label(schema, key, subject)


def _label(schema: Schema, key: str, subject: str | None) -> str:
    """'name' for the subject's own columns, 'category name' for related ones."""
    column = schema.column(key)
    if column.table == subject:
        return column.label
    owner = entity(schema, column.table)
    if column.label.startswith(owner) or set(owner.split()) <= {lemma(w) for w in column.label.split()}:
        return column.label          # 'per pupil expenditure', not 'expenditure per pupil expenditure'
    return f"{owner} {column.label}"


def _output(schema: Schema, item: OutputItem, subject: str | None) -> str:
    if item.star:
        return f"all details of the {plural(entity(schema, item.table or ''))}"
    if item.func == "PCT":
        counted = plural(entity(schema, item.table or ""))
        part = _join([condition(schema, c, item.table) for c in item.where])
        return f"the percentage of {counted} {part}".rstrip()
    if item.per:
        per = entity(schema, item.per)
        word = {"AVG": "average", "MAX": "largest", "MIN": "smallest"}.get(item.func or "", "total")
        if item.column is None:
            return (f"the {word} number of {plural(entity(schema, item.table or ''))} per {per} "
                    f"(counting {plural(per)} with none as 0)")
        return f"the {word} per-{per} total of the {_label(schema, item.column, subject)}"
    if item.column is None:
        return f"the number of {plural(entity(schema, item.table or ''))}"
    label = _label(schema, item.column, subject)
    if item.func is None or item.func == "EXPR":
        return label
    if item.func == "COUNT":
        return f"the number of different {label} values" if item.distinct else f"the number of {label} values"
    return f"the {FUNC[item.func]} {label}"


def _join(parts: list[str], word: str = "and") -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f" {word} " + parts[-1]


SIMPLE = ("compare", "text", "between", "in_values", "null", "period")


def condition(schema: Schema, cond: Condition, subject: str | None) -> str:
    """'whose price is ...' for the subject's own attributes; 'that has an order whose
    status is ...' for attributes of records related one-to-many."""
    if cond.kind in ("all", "any"):
        parts = [condition(schema, c, subject) for c in cond.inner]
        if cond.kind == "all" and parts and all(p.startswith("that has ") for p in parts):
            head = parts[0].split(" whose ")[0]
            return head + " whose " + " and ".join(p.split(" whose ", 1)[1] for p in parts)
        if cond.kind == "all" and all(p.startswith("whose ") for p in parts):
            return "whose " + " and ".join(p.removeprefix("whose ") for p in parts)
        return _join(parts, "and" if cond.kind == "all" else "or")
    table = cond.column.split(".")[0] if cond.column and cond.kind in SIMPLE else None
    if (table and subject and table != subject and table in schema.tables and subject in schema.tables
            and not schema.is_role(table) and schema.one_to_many(subject, table)):
        noun = entity(schema, table)
        article = "an" if noun[:1].lower() in "aeiou" else "a"
        return f"that has {article} {noun} {_condition(schema, cond, table)}"
    return _condition(schema, cond, subject)


def _condition(schema: Schema, cond: Condition, subject: str | None) -> str:
    kind = cond.kind
    if kind == "absent":
        what = plural(entity(schema, cond.table or ""))
        inner = _join([condition(schema, c, cond.table) for c in cond.inner])
        return f"that has no {what}" + (f" {_inner(inner)}" if inner else "")
    if kind == "related":
        return f"that has at least one {entity(schema, cond.table or '')}"
    if kind == "having":
        if cond.column is None:
            what = plural(entity(schema, cond.table or ""))
            return f"that has {COUNT_OPS.get(cond.op, cond.op)} {cond.values[0]} {what}"
        return (f"whose {FUNC.get(cond.func or 'SUM', 'total')} {_label(schema, cond.column, subject)} "
                f"{OPS[cond.op]} {_value(cond.values[0])}")
    label = _label(schema, cond.column, subject) if cond.column else "value"
    if kind == "compare" and cond.op in ("=", "!=") and cond.column:
        flag = flag_values(schema.values.get(cond.column, ()))
        if flag and str(cond.values[0]) in flag:
            positive = (str(cond.values[0]) == flag[0]) == (cond.op == "=")
            return f"that {'is' if positive else 'is not'} {schema.column(cond.column).label}"
    if kind == "compare" and cond.column and schema.column(cond.column).kind == DATE and _is_year(cond.values[0]):
        word = {"=": "in", "!=": "not in", ">": "after", "<": "before", ">=": "in or after",
                "<=": "in or before"}[cond.op]
        return f"whose {label} is {word} {cond.values[0]}"
    if kind in ("compare", "text"):
        return f"whose {label} {OPS[cond.op]} {_value(cond.values[0])}"
    if kind == "columns":
        word = {"=": "is the same as", "!=": "is different from"}.get(cond.op, OPS[cond.op])
        return f"whose {label} {word} the {_label(schema, cond.values[0], subject)}"
    if kind == "period":
        return f"whose {label} is in {cond.values[2]}"
    if kind == "between":
        return f"whose {label} is between {_value(cond.values[0])} and {_value(cond.values[1])}"
    if kind == "in_values":
        values = _join([_value(v) for v in cond.values], "or")
        return f"whose {label} is {'not ' if cond.negated else ''}{values}"
    if kind == "null":
        return f"whose {label} is {'recorded' if cond.negated else 'missing'}"
    if kind == "vs_aggregate":
        word = {"=": "equals", ">": "is greater than", "<": "is less than"}.get(cond.op, OPS[cond.op])
        func = FUNC.get(cond.func or "AVG", "average")
        if cond.op == "=" and cond.func in ("MAX", "MIN"):
            return f"whose {label} is the {'highest' if cond.func == 'MAX' else 'lowest'} of all"
        table = cond.col.split(".")[0]
        if cond.inner:
            inner = _join([condition(schema, c, table) for c in cond.inner])
            return f"whose {label} {word} the {func} {label} of the {plural(entity(schema, table))} {inner}"
        return f"whose {label} {word} the {func} {label} of all {plural(entity(schema, table))}"
    if kind == "having_vs" and cond.column is None:
        subject_name = entity(schema, cond.values[0])
        return (f"that has {'more' if cond.op == '>' else 'fewer'} {plural(entity(schema, cond.table or ''))} "
                f"than the average {subject_name}")
    if kind == "having_vs":
        return (f"whose average {label} is {'above' if cond.op == '>' else 'below'} the average "
                f"{label} of all {plural(entity(schema, cond.col.split('.')[0]))}")
    if kind == "vs_group":
        group = _group_label(schema, cond.table or "", cond.col.split(".")[0])
        if cond.op == "=" and cond.func in ("MAX", "MIN"):
            return f"whose {label} is the {'highest' if cond.func == 'MAX' else 'lowest'} within their {group}"
        return (f"whose {label} is {'above' if cond.op == '>' else 'below'} the average {label} "
                f"of their {group}")
    if kind == "same_as":
        inner = _join([condition(schema, c, cond.inner[0].column.split('.')[0] if cond.inner and
                                 cond.inner[0].column else subject).removeprefix("whose ")
                       for c in cond.inner])
        return f"with the same {label} as the one whose {inner}"
    if kind == "top_group":
        word = "most" if cond.func != "ASC" else "least"
        return f"whose {label} {'is' if cond.op == '=' else 'is not'} the {word} common {label}"
    return f"{kind} {label}"


def _inner(text: str) -> str:
    return text.replace("whose ", "whose ", 1)


def _order(schema: Schema, order: Ordering, subject: str | None) -> str:
    if order.column is None:
        return f"by number of {plural(entity(schema, order.table or ''))}, {'most' if order.desc else 'fewest'} first"
    label = _label(schema, order.column, subject)
    if order.func == "EXPR":
        return f"by {label}, {'highest' if order.desc else 'lowest'} first"
    if order.func:
        return f"by {FUNC[order.func]} {label}, {'highest' if order.desc else 'lowest'} first"
    column = schema.column(order.column)
    if column.kind == "text":
        return f"by {label} {'in reverse alphabetical order' if order.desc else 'alphabetically'}"
    if column.kind == "date":
        return f"by {label}, {'latest' if order.desc else 'earliest'} first"
    return f"by {label}, {'highest' if order.desc else 'lowest'} first"


def describe(schema: Schema, intent: Intent) -> str:
    subject = intent.subject or (intent.outputs[0].tables[0] if intent.outputs and intent.outputs[0].tables else None)
    subject_name = entity(schema, subject) if subject else "record"
    groups = _effective_groups(intent, schema)
    plain = [o for o in intent.outputs if not o.is_aggregate]
    aggregates = [o for o in intent.outputs if o.is_aggregate]
    where = [c for c in intent.conditions if c.kind not in ("having", "having_vs")]
    having = [c for c in intent.conditions if c.kind in ("having", "having_vs")]
    joiner = "and" if intent.connector == "AND" else "or"
    restrict = _join([condition(schema, c, subject) for c in where], joiner)

    only_count = len(intent.outputs) == 1 and intent.outputs[0].func == "COUNT" and not intent.outputs[0].column
    if only_count and not groups:
        counted = intent.outputs[0].table or subject or ""
        text = f"Count the {plural(entity(schema, counted))}"
        if restrict:
            text += " " + restrict
    elif aggregates and not plain and not groups and all(o.func == "PCT" or o.per for o in aggregates):
        text = "Give " + _join([_output(schema, o, subject) for o in aggregates])
        if restrict:
            text += f", counting only {plural(subject_name)} {restrict}"
    elif aggregates and not plain and not groups:
        text = "Give " + _join([_output(schema, o, subject) for o in aggregates])
        text += f" of the {plural(subject_name)}" + (f" {restrict}" if restrict else "")
    else:
        if groups:
            group_labels = [_group_label(schema, g, subject) for g in groups]
            shown = [o for o in intent.outputs if not (o.column in groups and not o.is_aggregate)]
            items = [_output(schema, o, subject) for o in shown]
            text = (f"For each {_join(group_labels)}, give " + _join(items) if items
                    else f"List each {_join(group_labels)}")
            if intent.keep_empty:
                text += " (including those with none)"
            if restrict:
                text += f", counting only {plural(subject_name)} {restrict}"
        else:
            items = [_output(schema, o, subject) for o in intent.outputs]
            prefix = "the different " if intent.distinct else "the "
            text = f"List {prefix}{_join(items)} of each {subject_name}"
            if restrict:
                text += f" {restrict}"
    if having:
        text += ", keeping only those " + _join([condition(schema, c, subject) for c in having])
    if intent.excluded:
        text += ", except those " + _join([condition(schema, c, subject) for c in intent.excluded])
    if intent.order:
        text += ", sorted " + ", then ".join(_order(schema, o, subject) for o in intent.order)
    if intent.limit is not None and intent.limit_per:
        text += (f", keeping only the top {intent.limit} within each "
                 f"{_group_label(schema, intent.limit_per, subject)}")
    elif intent.limit is not None:
        if intent.offset:
            text += f", skipping the first {intent.offset} and keeping the next {intent.limit}"
        else:
            text += ", keeping only the top one" if intent.limit == 1 else f", keeping only the top {intent.limit}"
    if intent.both is not None:
        text += " (both conditions must be met, possibly by different records)"
    return text + "."
