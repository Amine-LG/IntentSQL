"""Natural language -> Intent, using Jev only for bounded, grounded decisions.

Round 1 is one speculative fan-out: every decision the request might need is
asked at once, over candidates that code generates from the schema and the
request's grounded literals.  Alternatives that exclude each other compete in a
single Choice (never as independent yes/no questions, which over-select
near-synonyms).  Dependent decisions are asked speculatively for every parent
option and only the branch selected by the parent is consumed.

Code then assembles the answers into an ``Intent``.  Join paths, grouping keys,
subquery shapes and FK-equivalent column choices are derived deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from intentsql.grounding import Grounding, Literal
from intentsql.intent import Condition, Intent, OutputItem, Ordering, column_phrase
from intentsql.jev import Answer, Question, choice, noul
from intentsql.schema import BOOLEAN, DATE, NUMBER, TEXT, Column, Schema
from intentsql.text import ORDINALS

NUMERIC_OPS = {"=": "equal to", ">": "greater than (more, over, above, after, older)",
               "<": "less than (fewer, under, below, before, younger)",
               ">=": "at least (or more, no less than)", "<=": "at most (or less, no more than)",
               "!=": "not equal to"}
TEXT_OPS = {"=": "is exactly this value", "!=": "is not this value",
            "contains": "contains this text", "starts": "starts with this text",
            "ends": "ends with this text", "not_contains": "does not contain this text"}
AGG_FUNCS = (("AVG", "average"), ("SUM", "total"), ("MAX", "maximum"), ("MIN", "minimum"))
AGG_CUES = {
    "AVG": re.compile(r"\b(average|mean|avg)\b", re.I),
    "SUM": re.compile(r"\b(total|sum|combined|overall amount)\b", re.I),
    "MAX": re.compile(r"\b(max|maximum|highest|largest|biggest|most|greatest|latest|oldest|longest)\b", re.I),
    "MIN": re.compile(r"\b(min|minimum|lowest|smallest|least|fewest|earliest|youngest|shortest)\b", re.I),
}
DISTINCT_CUE = re.compile(r"\b(distinct|different|unique|differ\w*|various)\b", re.I)
NEGATION_CUE = re.compile(r"\b(not|no|never|without|none|except|excluding|neither|nor|other than|"
                          r"lacks?|apart from|besides)\b|n't\b", re.I)
AGG_COMPARE_CUE = re.compile(r"\b(average|mean|than (any|all|every|each|the (maximum|minimum|largest|"
                             r"smallest|highest|lowest|max|min))|than (that|those) of)\b", re.I)
CATEGORICAL_MAX = 12


@dataclass
class Option:
    key: str
    text: str
    payload: Any


@dataclass
class LiteralSlots:
    """Speculative decisions about one literal; the role selects which are used."""
    literal: Literal
    roles: list[Option]
    columns: list[Option]            # which column the value is compared with
    ops: dict[str, str]              # comparison wording
    count_tables: list[Option]       # whose records are counted (HAVING COUNT)
    group_funcs: list[Option]        # SUM/AVG/... for an aggregate threshold
    context: str = ""                # the literal with surrounding words


@dataclass
class Candidates:
    tables: list[str]
    classes: list[str]                                           # canonical column keys in scope
    counts: list[Option] = field(default_factory=list)
    forms: dict[str, list[Option]] = field(default_factory=dict)
    multi_agg: list[tuple[str, str]] = field(default_factory=list)  # (column, func) extra aggregates
    literals: dict[str, LiteralSlots] = field(default_factory=dict)
    categorical: dict[str, list[Option]] = field(default_factory=dict)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    vs_aggregate: list[Option] = field(default_factory=list)
    groups: list[Option] = field(default_factory=list)
    orders: list[Option] = field(default_factory=list)
    limits: list[Option] = field(default_factory=list)
    shares: list[Option] = field(default_factory=list)              # attributes a reference shares


@dataclass
class Decision:
    """One recorded Jev decision, for the debug view and failure analysis."""
    key: str
    purpose: str
    question: str
    options: dict[str, str] | None
    selected: Any
    probability: float
    probabilities: dict[str, float] | None = None


@dataclass
class Strategy:
    """Knobs for prompting experiments (see docs/EXPERIMENTS.md)."""
    include_schema_in_state: bool = True
    distinct_threshold: float = 0.5
    related_threshold: float = 0.85
    categorical_threshold: float = 0.6
    value_examples: int = 3


# ---- scope ---------------------------------------------------------------------------

def relevant_tables(g: Grounding, max_columns: int = 90) -> list[str]:
    """All tables for normal schemas; for very large ones, the lexically linked
    tables, the bridges between them and further tables up to a column budget."""
    schema = g.schema
    if len(schema.columns) <= max_columns:
        return list(schema.tables)

    def score(t: str) -> float:
        return g.table_link.get(t, 0) + max((g.column_link.get(c.key, 0)
                                            for c in schema.tables[t].columns), default=0)

    scored = sorted(schema.tables, key=lambda t: -score(t))
    keep = [t for t in scored if score(t) > 0][:8] or scored[:4]
    for table in list(keep):
        for other in schema.neighbors(table):
            if schema.is_bridge(other) and other not in keep:
                keep.append(other)
    total = sum(len(schema.tables[t].columns) for t in keep)
    for table in scored:
        if table in keep:
            continue
        size = len(schema.tables[table].columns)
        if total + size > max_columns:
            continue
        keep.append(table)
        total += size
    return keep


def _classes(schema: Schema, tables: list[str]) -> list[str]:
    """Canonical column keys for every value-equivalence class within scope."""
    seen: dict[str, str] = {}
    for table in tables:
        for column in schema.tables[table].columns:
            canon = schema.canonical(column.key)
            if canon.split(".")[0] not in tables:
                canon = next((m for m in schema.equivalents(column.key) if m.split(".")[0] in tables),
                             column.key)
            seen.setdefault(canon, canon)
    return list(seen)


def _describe_column(schema: Schema, key: str, examples: int) -> str:
    column = schema.column(key)
    phrase = column_phrase(schema, key)
    values = schema.values.get(key, ())
    if examples and column.kind in (TEXT, BOOLEAN) and values and not column.is_identifier:
        sample = ", ".join(repr(v) for v in values[:examples])
        return f"{phrase} (e.g. {sample})"
    return phrase


# ---- candidate generation ---------------------------------------------------------------

def build_candidates(g: Grounding, strategy: Strategy) -> Candidates:
    schema = g.schema
    tables = relevant_tables(g)
    classes = _classes(schema, tables)
    cands = Candidates(tables, classes)
    columns = [schema.column(k) for k in classes]

    # Outputs: one exclusive count choice, one exclusive form choice per column class.
    for table in tables:
        if not schema.is_bridge(table):
            cands.counts.append(Option(f"t{len(cands.counts)}",
                                       f"how many {schema.tables[table].label} records there are",
                                       OutputItem("COUNT", None, table)))
    for column in columns:
        phrase = column_phrase(schema, column.key)
        if column.kind in (TEXT, DATE, BOOLEAN) or (column.is_identifier and not column.primary_key):
            cands.counts.append(Option(f"t{len(cands.counts)}",
                                       f"how many different {phrase} values there are",
                                       OutputItem("COUNT", column.key, distinct=True)))
        forms = [Option("value", f"the {phrase} itself (one value per result row)",
                        OutputItem(None, column.key))]
        if column.is_numeric and not column.is_identifier:
            forms += [Option(func, f"the {word} {phrase}", OutputItem(func, column.key))
                      for func, word in AGG_FUNCS]
        cands.forms[column.key] = forms
    cue_funcs = [f for f, cue in AGG_CUES.items() if cue.search(g.question)]
    if len(cue_funcs) >= 2:
        for column in columns:
            if column.is_numeric and not column.is_identifier:
                cands.multi_agg.extend((column.key, f) for f in cue_funcs)

    for lit in g.literals:
        cands.literals[lit.id] = _literal_slots(g, lit, tables, classes)
    if any(lit.kind == "text" for lit in g.literals):
        cands.shares = [Option(f"h{i}", f"the same {column_phrase(schema, c.key)}", c.key)
                        for i, c in enumerate(c for c in columns if not c.primary_key)][:250]

    # Implicit categorical values (e.g. "male singers" -> is_male = 'T').
    literal_classes = {schema.canonical(k) for lit in g.literals for k in lit.columns}
    for column in columns:
        if column.is_identifier or column.key in literal_classes:
            continue
        values = schema.values.get(column.key, ())
        if column.kind in (TEXT, BOOLEAN) and 2 <= len(values) <= CATEGORICAL_MAX:
            phrase = column_phrase(schema, column.key)
            cands.categorical[column.key] = [Option(f"v{i}", f"only results whose {phrase} is '{v}'", v)
                                             for i, v in enumerate(values)]

    for subject in tables:
        if schema.is_bridge(subject):
            continue
        for target in _related_targets(schema, subject):
            if target in tables:
                cands.relations.append((f"r{len(cands.relations)}", subject, target))

    if AGG_COMPARE_CUE.search(g.question):
        for column in columns:
            if column.is_numeric and not column.is_identifier:
                phrase = column_phrase(schema, column.key)
                label = schema.tables[column.table].label
                for op, word in ((">", "greater than"), ("<", "less than")):
                    for func, fword in (("AVG", "average"), ("MAX", "maximum"), ("MIN", "minimum")):
                        cands.vs_aggregate.append(Option(
                            f"a{len(cands.vs_aggregate)}",
                            f"{phrase} {word} the {fword} {phrase} of all {label} records",
                            Condition("vs_aggregate", column.key, op, func=func)))
        cands.vs_aggregate = cands.vs_aggregate[:250]

    for column in columns:
        if column.kind in (TEXT, BOOLEAN, DATE) or column.is_identifier or (
                column.distinct_count is not None and column.distinct_count <= 30):
            cands.groups.append(Option(f"g{len(cands.groups)}",
                                       f"one group per distinct {column_phrase(schema, column.key)}",
                                       column.key))

    _order_candidates(g, cands, columns)

    cands.limits.append(Option("all", "no: return every result (or the single computed value)", None))
    cands.limits.append(Option("one", "only the single top-ranked item (e.g. 'the highest', 'the most', 'the first')", 1))
    for lit in g.literals:
        if lit.kind == "number" and isinstance(lit.value, int) and 1 < lit.value <= 1000:
            cands.limits.append(Option(f"n{lit.id}",
                                       f"only the top {lit.value} items (\"{lit.text}\" in the request)",
                                       lit.value))
    return cands


def _order_candidates(g: Grounding, cands: Candidates, columns: list[Column]) -> None:
    schema = g.schema
    orders: list[Option] = []

    def add(text: str, payload: Ordering) -> None:
        orders.append(Option(f"s{len(orders)}", text, payload))

    for table in cands.tables:
        if not schema.is_bridge(table):
            label = schema.tables[table].label
            add(f"by how many {label} records each result has, most first",
                Ordering("COUNT", None, table, True))
            add(f"by how many {label} records each result has, fewest first",
                Ordering("COUNT", None, table, False))
    for column in columns:
        if column.is_identifier and not column.primary_key:
            continue
        phrase = column_phrase(schema, column.key)
        if column.kind in (NUMBER, DATE):
            hi, lo = ("latest", "earliest") if column.kind == DATE else ("highest", "lowest")
            add(f"by {phrase}, {hi} first", Ordering(None, column.key, None, True))
            add(f"by {phrase}, {lo} first", Ordering(None, column.key, None, False))
            if column.is_numeric and not column.is_identifier:
                for func, word in (("SUM", "total"), ("AVG", "average")):
                    add(f"by the {word} {phrase} per group, highest first", Ordering(func, column.key, None, True))
                    add(f"by the {word} {phrase} per group, lowest first", Ordering(func, column.key, None, False))
        elif column.kind == TEXT:
            add(f"by {phrase} alphabetically (A to Z)", Ordering(None, column.key, None, False))
            add(f"by {phrase} in reverse alphabetical order (Z to A)", Ordering(None, column.key, None, True))
    if len(orders) > 250:
        def relevance(option: Option) -> float:
            o = option.payload
            key = o.column or ""
            return g.column_link.get(key, 0) + g.table_link.get(o.table or key.split(".")[0], 0)
        keep = {id(o) for o in sorted(orders, key=lambda o: -relevance(o))[:250]}
        orders = [o for o in orders if id(o) in keep]
    cands.orders = orders


def _related_targets(schema: Schema, subject: str) -> list[str]:
    targets = []
    for neighbor in schema.neighbors(subject):
        targets.append(neighbor)
        if schema.is_bridge(neighbor):
            targets.extend(far for far in schema.neighbors(neighbor) if far != subject)
    return list(dict.fromkeys(targets))


def literal_context(question: str, lit: Literal, before: int = 4, after: int = 3) -> str:
    """The literal with a few surrounding words, the literal itself in [brackets]."""
    left = question[:lit.start].split()[-before:]
    right = question[lit.end:].split()[:after]
    return " ".join(left + [f"[{question[lit.start:lit.end]}]"] + right)


def _literal_slots(g: Grounding, lit: Literal, tables: list[str], classes: list[str]) -> LiteralSlots:
    schema = g.schema
    in_scope = set(classes)

    def scoped(key: str) -> str:
        canon = schema.canonical(key)
        if canon in in_scope:
            return canon
        return next((m for m in schema.equivalents(key) if m in in_scope), key)

    columns: list[Option] = []
    if lit.kind == "text":
        stored = {}
        for key, value in lit.alternatives:
            stored.setdefault(scoped(key), value)
        keys = [k for k in stored if k.split(".")[0] in tables]
        if not keys:
            keys = [k for k in classes if schema.column(k).kind in (TEXT, DATE)
                    and not schema.column(k).is_identifier]
        for key in keys:
            value = stored.get(key, lit.value)
            columns.append(Option(f"c{len(columns)}",
                                  f"{_describe_column(schema, key, 2)} (value '{value}')",
                                  (key, value)))
        roles = [Option("value", "a value of an attribute that results are filtered by", "value"),
                 Option("reference", "it identifies one particular record, and results must share an "
                                     "attribute with that record (e.g. 'in the same department as "
                                     "Clara')", "reference"),
                 Option("other", "not a filter value (e.g. part of a name of a table or column, "
                                 "or a word used in another sense)", None)]
        ops = TEXT_OPS
    else:
        numeric = [k for k in classes if schema.column(k).kind in (NUMBER, DATE)
                   or k in {scoped(c) for c in lit.columns}]
        matched = {scoped(c) for c in lit.columns}
        numeric.sort(key=lambda k: (k not in matched, -g.column_link.get(k, 0),
                                    schema.column(k).is_identifier))
        for key in numeric[:200]:
            columns.append(Option(f"c{len(columns)}", _describe_column(schema, key, 0), (key, lit.value)))
        roles = [Option("value", "a value that a stored attribute of the results is compared "
                                 "with (e.g. 'older than 30', 'in 2014', 'priced 9.99')", "value"),
                 Option("count", "a threshold on how many related records each result has "
                                 "(e.g. 'at least 2 courses', 'more than 3 orders')", "count"),
                 Option("group_total", "a threshold on a total or average computed per group "
                                       "(e.g. 'whose total sales exceed 1000')", "group_total"),
                 Option("limit", "how many results to return (e.g. 'top 5', 'the 3 largest')", "limit"),
                 Option("other", "none of these (e.g. part of a name, an identifier inside text, "
                                 "or a column name such as '200 meters')", None)]
        ops = NUMERIC_OPS
    count_tables = [Option(f"k{i}", f"how many {schema.tables[t].label} records", t)
                    for i, t in enumerate(t for t in tables if not schema.is_bridge(t))]
    group_funcs = [Option(f, w, f) for f, w in AGG_FUNCS]
    return LiteralSlots(lit, roles, columns, dict(ops), count_tables, group_funcs,
                        literal_context(g.question, lit))


# ---- questions ---------------------------------------------------------------------------

def _state(g: Grounding, cands: Candidates, strategy: Strategy) -> dict[str, Any]:
    state: dict[str, Any] = {"request": g.question}
    if strategy.include_schema_in_state:
        state["database"] = {g.schema.tables[t].label: [c.label for c in g.schema.tables[t].columns]
                             for t in cands.tables}
    return state


def round_one(g: Grounding, cands: Candidates, strategy: Strategy) -> dict[str, Question]:
    schema = g.schema
    q: dict[str, Question] = {}
    q["count"] = choice(
        "Does `request` ask to be told a count (how many)? If so, a count of what? Ranking or "
        "filtering by a count ('the most', 'at least 3') does not by itself ask to show the count.",
        {**{o.key: o.text for o in cands.counts}, "none": "the request does not ask for a count"})
    for key, forms in cands.forms.items():
        phrase = column_phrase(schema, key)
        opts = {o.key: o.text for o in forms}
        opts["no"] = (f"the {phrase} is not asked to be shown (it may still be used to "
                      "filter, sort or group the results)")
        q[f"col_{key}"] = choice(
            f"Does `request` explicitly ask to show the {phrase}? If so, in which form?", opts)
    for key, func in cands.multi_agg:
        word = dict(AGG_FUNCS)[func]
        q[f"agg2_{key}_{func}"] = noul(
            f"Does `request` ask to show the {word} {column_phrase(schema, key)}?")
    negation = bool(NEGATION_CUE.search(g.question))
    for lit_id, slots in cands.literals.items():
        lit = slots.literal
        quoted = slots.context
        q[f"role_{lit_id}"] = choice(
            {"value": quoted, "question": "In `request`, what is the bracketed part of `value`?"},
            {o.key: o.text for o in slots.roles})
        if len(slots.columns) >= 2:
            q[f"lcol_{lit_id}"] = choice(
                {"value": quoted,
                 "question": "Which attribute does `request` compare with the bracketed part of `value`?"},
                {o.key: o.text for o in slots.columns})
        q[f"lop_{lit_id}"] = choice(
            {"value": quoted,
             "question": "How does `request` compare the attribute or count with the bracketed "
                         "part of `value`?"},
            slots.ops)
        if lit.kind == "number" and len(slots.count_tables) >= 2:
            q[f"lcnt_{lit_id}"] = choice(
                {"value": quoted, "question": "If the bracketed part of `value` limits how many "
                                              "related records each result has, records of which "
                                              "kind are counted?"},
                {o.key: o.text for o in slots.count_tables})
        if lit.kind == "number":
            q[f"lfun_{lit_id}"] = choice(
                {"value": quoted, "question": "If the bracketed part of `value` is a threshold on a "
                                              "per-group total or average, which aggregate is it?"},
                {o.key: o.text for o in slots.group_funcs})
        if lit.kind == "text" and len(cands.shares) >= 2:
            q[f"lshr_{lit_id}"] = choice(
                {"value": quoted, "question": "If the bracketed part of `value` identifies a record "
                                              "that results must resemble, which attribute must "
                                              "they share with it?"},
                {o.key: o.text for o in cands.shares})
        if negation:
            q[f"lexc_{lit_id}"] = noul(
                {"value": quoted,
                 "question": "Does `request` ask to exclude the results that match the bracketed "
                             "part of `value` (e.g. 'all X except those ...', 'X that never ...')?"})
    for key, options in cands.categorical.items():
        phrase = column_phrase(schema, key)
        opts = {o.key: o.text for o in options}
        opts["none"] = f"no restriction on {phrase}"
        q[f"cat_{key}"] = choice(
            f"Does `request` itself restrict the results to one particular {phrase}? "
            "Choose a value only when the request states that restriction.", opts)
    for key, subject, target in cands.relations:
        s_label, t_label = schema.tables[subject].label, schema.tables[target].label
        q[f"rel_{key}"] = choice(
            f"Does `request` restrict {s_label} records by whether they have related {t_label} records?",
            {"none": "no such restriction",
             "has": f"only {s_label} records that have at least one related {t_label} record",
             "lacks": f"only {s_label} records that have no related {t_label} record at all"})
    if cands.vs_aggregate:
        q["vsagg"] = choice(
            "Does `request` compare each result's value with an average, maximum or minimum "
            "computed over all records? If so, which comparison?",
            {**{o.key: o.text for o in cands.vs_aggregate}, "none": "no such comparison"})
    q["group"] = choice(
        "Does `request` ask for one result row per group (e.g. 'for each', 'per', 'by')? "
        "If so, what defines a group?",
        {**{o.key: o.text for o in cands.groups},
         "none": "no grouping: an overall result or a plain list of records"})
    q["order"] = choice(
        "Does `request` ask for the results to be sorted or ranked (including 'the most', "
        "'the highest', 'top')? If so, by what?",
        {**{o.key: o.text for o in cands.orders}, "unordered": "no ordering or ranking"})
    q["limit"] = choice(
        "Does `request` ask for only the top or first item(s) of a ranked list? If so, how many?",
        {o.key: o.text for o in cands.limits})
    if DISTINCT_CUE.search(g.question):
        q["distinct"] = noul("Does `request` ask for each value to be listed only once "
                             "(distinct, different or unique values)?")
    q["connector"] = choice(
        "If `request` states several conditions, must every result satisfy all of them, or "
        "is satisfying any one of them enough?",
        {"all": "all conditions must hold together (and)",
         "any": "satisfying any one condition is enough (or / either)"})
    return q


# ---- assembly -------------------------------------------------------------------------------

@dataclass
class Interpretation:
    intent: Intent | None
    decisions: list[Decision]
    uncertain: list[str]
    message: str | None = None


def _position(g: Grounding, item: OutputItem) -> int:
    if item.column:
        positions = [g.mention_position[k] for k in g.schema.equivalents(item.column)
                     if k in g.mention_position]
        if positions:
            return min(positions)
        return g.mention_position.get(item.column.split(".")[0], 10_000)
    return g.mention_position.get(item.table or "", 10_000) - 1


class _Recorder:
    def __init__(self) -> None:
        self.decisions: list[Decision] = []
        self.uncertain: list[str] = []

    def __call__(self, key: str, purpose: str, question: str, answer: Answer, selected: Any,
                 options: dict[str, str] | None = None) -> None:
        probability = answer.p if answer.kind == "noul" else answer.top_probability
        self.decisions.append(Decision(key, purpose, question, options, selected, probability or 0.0,
                                       dict(answer.probabilities) if answer.kind == "choice" else None))
        if answer.kind == "choice" and answer.margin() < 0.3:
            self.uncertain.append(key)


def _pick(options: list[Option], answer: Answer) -> Option:
    return next(o for o in options if o.key == answer.choice)


def assemble(g: Grounding, cands: Candidates, answers: dict[str, Answer],
             strategy: Strategy) -> Interpretation:
    schema = g.schema
    record = _Recorder()

    # -- outputs
    outputs: list[OutputItem] = []
    count_answer = answers["count"]
    if count_answer.choice != "none":
        option = _pick(cands.counts, count_answer)
        outputs.append(option.payload)
        record("count", "output", "count", count_answer, option.text)
    for key, forms in cands.forms.items():
        answer = answers[f"col_{key}"]
        if answer.choice == "no":
            continue
        option = _pick(forms, answer)
        outputs.append(option.payload)
        record(f"col_{key}", "output", column_phrase(schema, key), answer, option.key)
    extra: list[OutputItem] = []
    for key, func in cands.multi_agg:
        answer = answers[f"agg2_{key}_{func}"]
        item = OutputItem(func, key)
        if (answer.p or 0) >= 0.6 and item not in outputs:
            extra.append(item)
            record(f"agg2_{key}_{func}", "output", f"{func} {key}", answer, True)
    for item in extra:
        others = [o for o in outputs + extra if o.column == item.column and o.func and o != item]
        if others:
            outputs = [o for o in outputs if not (o.column == item.column and o.func is None)]
            outputs.append(item)
    if not outputs:
        ranked = sorted(((k[4:], 1 - a.probabilities.get("no", 1.0)) for k, a in answers.items()
                         if k.startswith("col_")), key=lambda kv: -kv[1])
        if not ranked:
            return Interpretation(None, record.decisions, record.uncertain,
                                  "Could not identify what the request asks to return.")
        outputs.append(OutputItem(None, ranked[0][0]))

    # -- literal-bound conditions
    conditions: list[Condition] = []
    excluded: list[Condition] = []
    limit: int | None = None
    for lit_id, slots in cands.literals.items():
        lit = slots.literal
        role_answer = answers[f"role_{lit_id}"]
        role = _pick(slots.roles, role_answer).payload
        record(f"role_{lit_id}", "literal role", f"role of {slots.context}", role_answer, role,
               {o.key: o.text for o in slots.roles})
        if role is None:
            continue
        op = answers[f"lop_{lit_id}"].choice
        if role == "limit":
            if isinstance(lit.value, int):
                limit = lit.value
            continue
        exclude = (answers[f"lexc_{lit_id}"].p or 0) >= 0.6 if f"lexc_{lit_id}" in answers else False
        if exclude:
            record(f"lexc_{lit_id}", "exclusion", f"exclude {slots.context}", answers[f"lexc_{lit_id}"], True)
        if role == "count":
            table = (_pick(slots.count_tables, answers[f"lcnt_{lit_id}"]).payload
                     if f"lcnt_{lit_id}" in answers else
                     (slots.count_tables[0].payload if slots.count_tables else None))
            if table is None:
                continue
            conditions.append(Condition("having", None, _numeric_op(op), (lit.value,), func="COUNT",
                                        table=table))
            record(f"lcnt_{lit_id}", "literal count", f"count of {table}",
                   answers.get(f"lcnt_{lit_id}", role_answer), table)
            continue
        if not slots.columns:
            continue
        column_answer = answers.get(f"lcol_{lit_id}")
        column_option = _pick(slots.columns, column_answer) if column_answer else slots.columns[0]
        key, value = column_option.payload
        if column_answer:
            record(f"lcol_{lit_id}", "literal column", f"column for {slots.context}", column_answer,
                   key, {o.key: o.text for o in slots.columns})
        if role == "group_total":
            func = answers[f"lfun_{lit_id}"].choice
            conditions.append(Condition("having", key, _numeric_op(op), (value,), func=func))
            continue
        if lit.kind == "text":
            kind = "text" if op in ("contains", "starts", "ends", "not_contains") else "compare"
            condition = Condition(kind, key, op, (value,))
        else:
            condition = Condition("compare", key, _numeric_op(op), (value,))
        record(f"lop_{lit_id}", "literal operator", f"operator for {slots.context}",
               answers[f"lop_{lit_id}"], op)
        if role == "reference" and f"lshr_{lit_id}" in answers:
            shared = _pick(cands.shares, answers[f"lshr_{lit_id}"]).payload
            record(f"lshr_{lit_id}", "shared attribute", f"shared with {slots.context}",
                   answers[f"lshr_{lit_id}"], shared)
            if shared != key:
                conditions.append(Condition("same_as", shared, inner=(condition,)))
                continue
        if exclude and condition.op in ("=", "contains", "starts", "ends", ">", "<", ">=", "<="):
            excluded.append(condition)
        else:
            conditions.append(condition)

    # -- implicit categorical values
    for key, options in cands.categorical.items():
        answer = answers[f"cat_{key}"]
        if answer.choice == "none" or answer.top_probability < strategy.categorical_threshold:
            continue
        option = _pick(options, answer)
        record(f"cat_{key}", "implicit value", option.text, answer, option.payload)
        conditions.append(Condition("compare", key, "=", (option.payload,)))

    # -- comparison with an aggregate
    if "vsagg" in answers and answers["vsagg"].choice != "none":
        option = _pick(cands.vs_aggregate, answers["vsagg"])
        record("vsagg", "aggregate comparison", option.text, answers["vsagg"], option.text)
        conditions.append(option.payload)

    # -- grouping, ordering, limit, distinct, connector
    group_by: list[str] = []
    if answers["group"].choice != "none":
        option = _pick(cands.groups, answers["group"])
        group_by.append(option.payload)
        record("group", "grouping", option.text, answers["group"], option.payload)
    order: tuple[Ordering, ...] = ()
    if answers["order"].choice != "unordered":
        option = _pick(cands.orders, answers["order"])
        order = (option.payload,)
        record("order", "ordering", option.text, answers["order"], option.text)
    limit_option = _pick(cands.limits, answers["limit"])
    record("limit", "limit", limit_option.text, answers["limit"], limit_option.payload)
    if limit_option.payload is not None:
        limit = limit_option.payload
    if not order:
        limit = None
    distinct = False
    if "distinct" in answers:
        distinct = (answers["distinct"].p or 0) >= strategy.distinct_threshold
        record("distinct", "distinct", "list each value once", answers["distinct"], distinct)
    connector = "OR" if answers["connector"].choice == "any" else "AND"

    # -- deterministic shaping
    outputs = _shape_outputs(schema, outputs, order, limit, group_by)
    outputs.sort(key=lambda item: _position(g, item))
    output_tables = {t for o in outputs for t in o.tables}

    # -- relationships
    subject = outputs[0].tables[0] if outputs and outputs[0].tables else None
    for key, rel_subject, target in cands.relations:
        answer = answers[f"rel_{key}"]
        if answer.choice == "none":
            continue
        if rel_subject not in output_tables and rel_subject != subject:
            continue
        if answer.choice == "has":
            if answer.top_probability < strategy.related_threshold:
                continue
            record(f"rel_{key}", "relationship", f"{rel_subject} has {target}", answer, "has")
            conditions.append(Condition("related", None, table=target))
        else:
            record(f"rel_{key}", "relationship", f"{rel_subject} lacks {target}", answer, "lacks")
            far = _far_side(schema, rel_subject, target)
            inner = [c for c in conditions if c.column and c.column.split(".")[0] in far
                     and c.column.split(".")[0] not in output_tables]
            conditions = [c for c in conditions if c not in inner]
            conditions.append(Condition("absent", None, table=target, inner=tuple(inner)))
            subject = rel_subject

    conditions, connector, both = _normalize_conditions(g, conditions, connector, subject)
    aggregated = (any(o.is_aggregate for o in outputs) or any(o.is_aggregate for o in order)
                  or any(c.kind == "having" for c in conditions))
    if not aggregated:
        group_by = []          # "the name of each program" lists rows; it does not group them
    if group_by:
        group_classes = set(schema.equivalents(group_by[0]))
        outputs = [OutputItem("COUNT", None, o.column.split(".")[0])
                   if o.func == "COUNT" and o.distinct and o.column in group_classes else o
                   for o in outputs]
        if not any(o.column in group_classes for o in outputs if o.column and not o.is_aggregate):
            label = _group_label(schema, group_by[0], outputs)
            if label:
                outputs.insert(0, OutputItem(None, label))
        outputs = [o for i, o in enumerate(outputs) if o not in outputs[:i]]
    offset = _ordinal_offset(g.question) if order and limit == 1 else None
    if any(o.is_aggregate for o in outputs) and not group_by and not any(
            not o.is_aggregate for o in outputs):
        distinct = False

    intent = Intent(outputs=tuple(outputs), conditions=tuple(conditions),
                    connector=connector, group_by=tuple(group_by), order=order, limit=limit,
                    offset=offset, distinct=distinct and not group_by, both=both, subject=subject,
                    excluded=tuple(excluded))
    intent = rebind_equivalents(schema, intent)
    return Interpretation(intent, record.decisions, record.uncertain)


def _group_label(schema: Schema, key: str, outputs: list[OutputItem]) -> str | None:
    """How to show a group: its key, or the entity's name when grouping by an id."""
    column = schema.column(key)
    if not column.is_identifier:
        return key
    entity = schema.table(column.table)
    for member in schema.equivalents(key):
        table = schema.table(member.split(".")[0])
        if schema.column(member).primary_key:
            entity = table
    if any(o.column and o.column.split(".")[0] == entity.name and not o.is_aggregate for o in outputs):
        return None   # the entity is already shown through one of its attributes
    for col in entity.columns:
        if col.kind == TEXT and re.search(r"\b(name|title)\b", col.label):
            return col.key
    return key


def _numeric_op(op: str | None) -> str:
    return op if op in NUMERIC_OPS else "="


def _shape_outputs(schema: Schema, outputs: list[OutputItem], order: tuple[Ordering, ...],
                   limit: int | None, group_by: list[str]) -> list[OutputItem]:
    """Superlative rows: 'the model with the least RAM, and its RAM' -> plain values
    under ORDER BY ... LIMIT, not MIN() aggregates with an accidental GROUP BY."""
    if order and limit is not None and not group_by and not order[0].is_aggregate:
        has_plain = any(not o.is_aggregate for o in outputs)
        if has_plain:
            outputs = [OutputItem(None, o.column) if o.func in ("MIN", "MAX") and o.column else o
                       for o in outputs]
    deduped: list[OutputItem] = []
    for item in outputs:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _far_side(schema: Schema, subject: str, target: str) -> set[str]:
    paths = schema.shortest_paths(subject, target, limit=1)
    return {step.table for step in paths[0]} if paths else {target}


def _normalize_conditions(g: Grounding, conditions: list[Condition], connector: str,
                          subject: str | None) -> tuple[list[Condition], str, tuple[int, int] | None]:
    """Deterministic clean-ups that need no semantic judgment."""
    if re.search(r"\bbetween\b", g.question, re.I):
        lows = [c for c in conditions if c.kind == "compare" and c.op in (">=", ">", "=")]
        highs = [c for c in conditions if c.kind == "compare" and c.op in ("<=", "<", "=")]
        for low in lows:
            for high in highs:
                if (low is not high and low.column == high.column and low in conditions
                        and high in conditions and isinstance(low.values[0], (int, float))
                        and isinstance(high.values[0], (int, float))):
                    a, b = low.values[0], high.values[0]
                    conditions = [c for c in conditions if c not in (low, high)]
                    conditions.append(Condition("between", low.column, values=(min(a, b), max(a, b))))
    unique: list[Condition] = []
    for c in conditions:
        if c not in unique:
            unique.append(c)
    conditions = unique
    both = None
    by_column: dict[str, list[Condition]] = {}
    for c in conditions:
        if c.kind == "compare" and c.op == "=":
            by_column.setdefault(g.schema.canonical(c.column), []).append(c)
    for conds in by_column.values():
        if len(conds) == 2 and conds[0].values != conds[1].values and connector == "AND":
            table = conds[0].column.split(".")[0]
            if subject is None or table == subject:
                # One row cannot hold two values: "from France and Germany" means either.
                others = [c for c in conditions if c not in conds]
                merged = Condition("in_values", conds[0].column, values=tuple(c.values[0] for c in conds))
                conditions = others + [merged]
            else:
                both = (conditions.index(conds[0]), conditions.index(conds[1]))
    return conditions, connector, both


def _ordinal_offset(question: str) -> int | None:
    match = re.search(r"\b(second|third|fourth|fifth)\s+(\w+est|most|highest|lowest|largest|"
                      r"smallest|youngest|oldest)\b", question, re.I)
    return ORDINALS[match.group(1).lower()] - 1 if match else None


# ---- FK-equivalent columns ------------------------------------------------------------

def rebind_equivalents(schema: Schema, intent: Intent) -> Intent:
    """Use, for each FK-equivalent column, the member that keeps the join tree smallest.

    Anchors are tables that are required regardless (counted tables, columns with
    no equivalent, relationship targets).  Each flexible column then takes the
    member located in an anchored table (the most-referenced one), if any.
    """
    keys: list[str] = []
    for item in intent.outputs:
        if item.column:
            keys.append(item.column)
    for cond in intent.conditions:
        if cond.column and cond.kind not in ("absent", "same_as"):
            keys.append(cond.column)
    keys.extend(intent.group_by)
    keys.extend(o.column for o in intent.order if o.column)
    weight: dict[str, int] = {}

    def anchor(table: str) -> None:
        weight[table] = weight.get(table, 0) + 1

    if any(c.kind == "absent" for c in intent.conditions) and intent.subject:
        anchor(intent.subject)
    for item in intent.outputs:
        if item.column is None and item.table:
            anchor(item.table)
    for cond in intent.conditions:
        if cond.kind in ("related", "having") and cond.table and not cond.column:
            anchor(cond.table)
        if cond.kind == "same_as" and cond.column:
            anchor(cond.column.split(".")[0])
    for key in keys:
        if len(schema.equivalents(key)) == 1:
            anchor(key.split(".")[0])
    mapping: dict[str, str] = {}
    for key in keys:
        members = schema.equivalents(key)
        if len(members) == 1:
            continue
        inside = sorted((m for m in members if m.split(".")[0] in weight),
                        key=lambda m: -weight[m.split(".")[0]])
        chosen = inside[0] if inside else key
        if not inside:
            anchor(key.split(".")[0])
        mapping[key] = chosen
    if not mapping:
        return intent

    def m(key: str | None) -> str | None:
        return mapping.get(key, key) if key else key

    outputs = tuple(OutputItem(o.func, m(o.column), o.table, o.distinct) for o in intent.outputs)
    conditions = tuple(Condition(c.kind, m(c.column), c.op, c.values, c.negated, c.func, c.table,
                                 c.inner) for c in intent.conditions)
    order = tuple(Ordering(o.func, m(o.column), o.table, o.desc) for o in intent.order)
    subject = intent.subject
    if outputs and outputs[0].tables and not any(c.kind == "absent" for c in conditions):
        subject = outputs[0].tables[0]
    return intent.with_(outputs=outputs, conditions=conditions,
                        group_by=tuple(m(k) for k in intent.group_by), order=order, subject=subject)


# ---- entry point -------------------------------------------------------------------------

AskFn = Callable[[dict[str, Any], dict[str, Question], str], dict[str, Answer]]


def interpret(g: Grounding, ask: AskFn, strategy: Strategy | None = None
              ) -> tuple[Interpretation, Candidates]:
    strategy = strategy or Strategy()
    cands = build_candidates(g, strategy)
    questions = round_one(g, cands, strategy)
    answers = ask(_state(g, cands, strategy), questions, "interpretation")
    return assemble(g, cands, answers, strategy), cands
