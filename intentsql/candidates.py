"""Candidate generation: every grounded reading a request might need, rendered as options.

Code builds all options from the inspected schema and the request's literals; Jev only
chooses among them (see interpret.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from intentsql.cues import (
    AGG_COMPARE_CUE,
    AGG_FUNCS,
    AGG_WORDS,
    CATEGORICAL_MAX,
    COLUMN_COMPARE_CUE,
    COMPARATIVE_CUE,
    CONTAINS_CUE,
    COUNT_CUE,
    COUNT_KINDS_CUE,
    DATE_OPS,
    DISTINCT_CUE,
    EXACT_TEXT_OPS,
    EXPLICIT_AGG,
    NULL_CUE,
    NUMERIC_OPS,
    OWN_GROUP_CUE,
    PARTIAL_TEXT_OPS,
    PER_GROUP_SUPERLATIVE,
    PERIOD_OPS,
    QUANTIFIED_CUE,
    STAR_CUE,
    TOP_GROUP_CUE,
)
from intentsql.grounding import Grounding, Literal
from intentsql.intent import Condition, Ordering, OutputItem, column_phrase, entity
from intentsql.schema import BOOLEAN, DATE, NUMBER, TEXT, Column, Schema, flag_values


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
class OutputGroup:
    """One exclusive output decision about one column class."""
    key: str
    phrase: str
    options: list[Option]


@dataclass
class Candidates:
    tables: list[str]
    classes: list[str]                                           # representative column keys in scope
    counts: list[Option] = field(default_factory=list)
    outputs: list[OutputGroup] = field(default_factory=list)
    multi_agg: list[tuple[str, str]] = field(default_factory=list)  # (column, func) extra aggregates
    literals: dict[str, LiteralSlots] = field(default_factory=dict)
    categorical: dict[str, list[Option]] = field(default_factory=dict)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    vs_aggregate: list[Option] = field(default_factory=list)
    groups: list[Option] = field(default_factory=list)
    orders: list[Option] = field(default_factory=list)
    limits: list[Option] = field(default_factory=list)
    shares: list[Option] = field(default_factory=list)
    joins: dict[str, list[Option]] = field(default_factory=dict)   # table pair -> FK choices
    nulls: list[Option] = field(default_factory=list)              # missing / present values
    stars: list[Option] = field(default_factory=list)              # "all information about X"
    multi_count: list[Option] = field(default_factory=list)        # several counts requested
    col_compares: list[Option] = field(default_factory=list)       # one column against another


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
    exclusion_threshold: float = 0.6
    value_examples: int = 3
    debias_order: bool = False          # ask each Choice twice with reversed options
    state_values: bool = False          # sample stored values in the schema description
    state_relations: bool = False       # list FK relationships in the state
    output_phrase: bool = False         # ask output questions against the extracted 'asked for' phrase
    verify: bool = True                 # verification round: check the reading, compare alternatives


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
            if (schema.is_bridge(other) or schema.is_role(other)) and other not in keep:
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


def _classes(g: Grounding, tables: list[str]) -> list[str]:
    """Columns to ask about.  Identifier columns linked by FK chains (Students.student_id
    -> People.person_id) are one candidate, shown under the member whose words best
    match the request.  Descriptive columns stay separate, because the same value in
    different tables plays different roles (a student's department vs a course's)."""
    schema = g.schema
    covered: set[str] = set()
    reps: list[str] = []
    for table in tables:
        for column in schema.tables[table].columns:
            if column.key in covered:
                continue
            members = [column.key]
            if schema.is_key(column.key):
                members += [m for m in schema.equivalents(column.key)
                            if m != column.key and m.split(".")[0] in tables
                            and schema.is_key(m) and m not in covered]
            covered.update(members)
            canon = schema.canonical(column.key)
            reps.append(max(members, key=lambda m: (g.column_link.get(m, 0),
                                                    g.table_link.get(m.split(".")[0], 0),
                                                    m == canon)))
    return reps


def _table_relevance(g: Grounding, table: str) -> float:
    columns = g.schema.tables[table].columns
    return (2 * g.table_link.get(table, 0)
            + max((g.column_link.get(c.key, 0) for c in columns), default=0))


def _by_relevance(g: Grounding, tables: list[str]) -> list[str]:
    """Tables the request mentions first.  Jev leans toward the first options of a
    Choice; ordering by relevance makes that lean harmless instead of misleading."""
    order = {t: i for i, t in enumerate(tables)}
    return sorted(tables, key=lambda t: (-_table_relevance(g, t), order[t]))


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
    classes = _classes(g, tables)
    cands = Candidates(tables, classes)
    columns = [schema.column(k) for k in classes]

    # Counts: one exclusive choice.
    for table in _by_relevance(g, tables):
        if schema.countable(table):
            cands.counts.append(Option(f"t{len(cands.counts)}",
                                       f"how many {entity(schema, table)} records there are",
                                       OutputItem("COUNT", None, table)))
    if EXPLICIT_AGG["AVG"].search(g.question):
        # 'the average number of orders per customer' (customers without orders count as 0).
        for parent in _by_relevance(g, tables):
            for child in tables:
                if (parent != child and g.table_link.get(parent, 0) > 0 and g.table_link.get(child, 0) > 0
                        and schema.countable(parent) and schema.countable(child)
                        and schema.one_to_many(parent, child)):
                    cands.counts.append(Option(
                        f"t{len(cands.counts)}",
                        f"the average number of {entity(schema, child)} records per {entity(schema, parent)}",
                        OutputItem("AVG", None, child, per=parent)))
    if DISTINCT_CUE.search(g.question) or COUNT_KINDS_CUE.search(g.question):
        # Counting distinct values only when the request says so ('how many different
        # cities'); otherwise these options pull counts toward the filtered column.
        for column in columns:
            if column.kind in (TEXT, DATE, BOOLEAN) or (column.is_identifier and not column.primary_key):
                cands.counts.append(Option(f"t{len(cands.counts)}",
                                           f"how many different {column_phrase(schema, column.key)} values there are",
                                           OutputItem("COUNT", column.key, distinct=True)))

    if STAR_CUE.search(g.question):
        cands.stars = [Option(f"w{i}", f"every attribute of the {entity(schema, t)} records",
                              OutputItem(None, None, t, star=True))
                       for i, t in enumerate(t for t in tables if schema.countable(t))]
    if len(COUNT_CUE.findall(g.question)) >= 2:
        cands.multi_count = [o for o in cands.counts if o.payload.column is None]

    # Outputs: one exclusive choice per column class.
    for i, column in enumerate(columns):
        phrase = column_phrase(schema, column.key)
        if column.aggregated:
            # Already a per-record aggregate: shown as is (the 'value' of each group).
            cands.outputs.append(OutputGroup(f"o{i}", phrase, [Option(
                "value", f"the {phrase} (one value per result row)", OutputItem("EXPR", column.key))]))
            continue
        options = [Option("value", f"the {phrase} itself (one value per result row)",
                          OutputItem(None, column.key))]
        if column.is_numeric and not column.is_identifier:
            options += [Option(func, f"the {word} {phrase}", OutputItem(func, column.key))
                        for func, word in AGG_FUNCS]
            options += _nested_options(g, column, phrase, tables)
        cands.outputs.append(OutputGroup(f"o{i}", phrase, options))
    cue_funcs = [f for f, cue in AGG_WORDS.items() if cue.search(g.question)]
    if len(cue_funcs) >= 2 and any(cue.search(g.question) for cue in EXPLICIT_AGG.values()) or len(
            [f for f, cue in EXPLICIT_AGG.items() if cue.search(g.question)]) >= 2 or (
            re.search(r"\b(highest|largest|maximum|maxium|maximal|max|longest)\b.*\b(lowest|smallest|min[iu]mum|mininum|minimal|min|shortest)\b|"
                      r"\b(lowest|smallest|min[iu]mum|mininum|minimal|min|shortest)\b.*\b(highest|largest|maximum|maxium|maximal|max|longest)\b",
                      g.question, re.I)):
        for column in columns:
            if column.is_numeric and not column.is_identifier:
                cands.multi_agg.extend((column.key, f) for f in cue_funcs)

    for lit in g.literals:
        cands.literals[lit.id] = _literal_slots(g, lit, tables, classes)
    if any(lit.kind == "text" for lit in g.literals):
        cands.shares = [Option(f"h{i}", f"the same {column_phrase(schema, c.key)}", c.key)
                        for i, c in enumerate(c for c in columns if not c.primary_key)][:250]

    # Implicit categorical values (e.g. "male singers" -> is_male = 'T').
    literal_classes = {k for lit in g.literals for m in lit.columns for k in schema.equivalents(m)}
    for column in columns:
        if column.is_identifier or column.key in literal_classes:
            continue
        values = schema.values.get(column.key, ())
        if (column.kind in (TEXT, BOOLEAN) or column.kind == NUMBER) and 2 <= len(values) <= CATEGORICAL_MAX:
            phrase = column_phrase(schema, column.key)
            flag = flag_values(values)
            if flag:
                yes, no = flag
                plural_entity = entity(schema, column.table) + "s"
                cands.categorical[column.key] = [
                    Option("v0", f"only {plural_entity} that are {column.label} ({column.label} = {yes})", yes),
                    Option("v1", f"only {plural_entity} that are not {column.label} ({column.label} = {no})", no)]
            else:
                cands.categorical[column.key] = [Option(f"v{i}", f"only results whose {phrase} is '{v}'", v)
                                                 for i, v in enumerate(values)]

    for subject in tables:
        if not schema.countable(subject):
            continue
        for target in _related_targets(schema, subject):
            if target in tables:
                cands.relations.append((f"r{len(cands.relations)}", subject, target))

    compared_with_subset = any(_after_than(g.question, lit) for lit in g.literals)
    if (AGG_COMPARE_CUE.search(g.question) or compared_with_subset) and COMPARATIVE_CUE.search(g.question):
        for column in columns:
            if column.is_numeric and not column.is_identifier:
                phrase = column_phrase(schema, column.key)
                label = entity(schema, column.table)
                for op, word in ((">", "greater than"), ("<", "less than")):
                    for func, fword in (("AVG", "average"), ("MAX", "maximum"), ("MIN", "minimum")):
                        cands.vs_aggregate.append(Option(
                            f"a{len(cands.vs_aggregate)}",
                            f"each result's {phrase} is {word} the {fword} {phrase} of all {label} records",
                            Condition("vs_aggregate", column.key, op, func=func)))
                    cands.vs_aggregate.append(Option(
                        f"a{len(cands.vs_aggregate)}",
                        f"each group's average {phrase} is {word} the average {phrase} of all {label} records",
                        Condition("having_vs", column.key, op, func="AVG")))
        for subject in _by_relevance(g, tables):
            if not schema.countable(subject):
                continue
            for target in _related_targets(schema, subject):
                if target in tables and schema.countable(target) and schema.one_to_many(subject, target):
                    s_label, t_label = entity(schema, subject), entity(schema, target)
                    for op, word in ((">", "more"), ("<", "fewer")):
                        cands.vs_aggregate.append(Option(
                            f"a{len(cands.vs_aggregate)}",
                            f"each {s_label} has {word} {t_label} records than the average {s_label}",
                            Condition("having_vs", None, op, (subject,), func="COUNT", table=target)))
        cands.vs_aggregate = cands.vs_aggregate[:250]

    if NULL_CUE.search(g.question):
        for column in columns:
            if (column.null_count or column.blank_count) and not column.primary_key:
                phrase = column_phrase(schema, column.key)
                cands.nulls.append(Option(f"n{len(cands.nulls)}", f"only results whose {phrase} is missing (empty, unknown)",
                                          Condition("null", column.key)))
                cands.nulls.append(Option(f"n{len(cands.nulls)}", f"only results whose {phrase} is present (recorded)",
                                          Condition("null", column.key, negated=True)))
        cands.nulls = cands.nulls[:250]

    if (AGG_COMPARE_CUE.search(g.question) and COMPARATIVE_CUE.search(g.question)
            and OWN_GROUP_CUE.search(g.question)) or PER_GROUP_SUPERLATIVE.search(g.question):
        for column in columns:
            if not column.is_numeric or schema.is_key(column.key):
                continue
            phrase = column_phrase(schema, column.key)
            for other in schema.tables[column.table].columns:
                if other.primary_key or other.key == column.key or other.is_numeric and not schema.is_key(other.key):
                    continue
                if not (schema.is_key(other.key) or (other.distinct_count or 0) <= 50):
                    continue
                group_phrase = column_phrase(schema, other.key)
                for op, word in ((">", "greater than"), ("<", "less than")):
                    cands.vs_aggregate.append(Option(
                        f"a{len(cands.vs_aggregate)}",
                        f"each result's {phrase} is {word} the average {phrase} of records with the same {group_phrase}",
                        Condition("vs_group", column.key, op, func="AVG", table=other.key)))
                for func, word in (("MAX", "highest"), ("MIN", "lowest")):
                    cands.vs_aggregate.append(Option(
                        f"a{len(cands.vs_aggregate)}",
                        f"each result has the {word} {phrase} among records with the same {group_phrase}",
                        Condition("vs_group", column.key, "=", func=func, table=other.key)))
        cands.vs_aggregate = cands.vs_aggregate[:250]

    if TOP_GROUP_CUE.search(g.question):
        for column in columns:
            if column.kind == TEXT and not column.primary_key and (column.distinct_count or 0) >= 2:
                phrase = column_phrase(schema, column.key)
                for desc, word in ((True, "most common"), (False, "least common")):
                    for negated in (False, True):
                        cands.vs_aggregate.append(Option(
                            f"a{len(cands.vs_aggregate)}",
                            f"results whose {phrase} {'is not' if negated else 'is'} the {word} {phrase}",
                            Condition("top_group", column.key, "!=" if negated else "=",
                                      func="DESC" if desc else "ASC")))
        cands.vs_aggregate = cands.vs_aggregate[:250]

    for column in sorted(columns, key=lambda c: -(g.column_link.get(c.key, 0)
                                                  + _table_relevance(g, c.table) / 4)):
        if column.kind in (TEXT, BOOLEAN, DATE) or column.is_identifier or (
                column.distinct_count is not None and column.distinct_count <= 30):
            cands.groups.append(Option(f"g{len(cands.groups)}",
                                       f"one group per distinct {column_phrase(schema, column.key)}",
                                       column.key))

    _order_candidates(g, cands, columns)
    if COLUMN_COMPARE_CUE.search(g.question):
        cands.col_compares = _column_comparisons(g, columns)

    cands.limits.append(Option("all", "no: return every result (or the single computed value)", None))
    cands.limits.append(Option("one", "only the single top-ranked item (e.g. 'the highest', 'the most', 'the first')", 1))
    for lit in g.literals:
        if lit.kind == "number" and isinstance(lit.value, int) and 1 < lit.value <= 1000:
            cands.limits.append(Option(f"n{lit.id}",
                                       f"only the top {lit.value} items (\"{lit.text}\" in the request)",
                                       lit.value))

    # Parallel relationships between the same two tables (e.g. source/destination airport).
    for i, a in enumerate(tables):
        for b in tables[i + 1:]:
            edges = schema.parallel_edges(a, b)
            if len(edges) >= 2:
                opts: list[Option] = []
                for fk in edges:
                    child = entity(schema, fk.table)
                    via = " and ".join(schema.table(fk.table).column(c).label for c in fk.columns)
                    opts.append(Option(f"j{len(opts)}",
                                       f"the {entity(schema, fk.ref_table)} given by the {child}'s {via}",
                                       fk.key))
                cands.joins["|".join(sorted((a, b)))] = opts
    return cands


def _nested_options(g: Grounding, column: Column, phrase: str, tables: list[str]) -> list[Option]:
    """'the average order value': an aggregate over parent records (orders) of each one's
    total of a child attribute (order lines), offered when the request names the parent."""
    schema = g.schema
    options = []
    funcs = [(f, w) for f, w in (("AVG", "average"), ("MAX", "largest"), ("MIN", "smallest"))
             if AGG_WORDS[f].search(g.question)]
    for parent in tables:
        if (parent == column.table or g.table_link.get(parent, 0) <= 0 or not schema.countable(parent)
                or not schema.one_to_many(parent, column.table)):
            continue
        name = entity(schema, parent)
        for func, word in funcs:
            options.append(Option(f"{func}per{parent}",
                                  f"the {word} per-{name} total of the {phrase} (add up each {name}'s "
                                  f"{phrase}, then take the {word} over {name}s)",
                                  OutputItem(func, column.key, per=parent)))
    return options


def _column_comparisons(g: Grounding, columns: list[Column]) -> list[Option]:
    """'candidates with a lower support rate than oppose rate', 'reviewers with the same
    name as the director': two attributes the request names, compared with each other."""
    schema = g.schema
    linked = [c for c in columns if g.column_link.get(c.key, 0) >= 0.5 and not c.is_identifier
              and c.formula is None]
    pairs: list[tuple[Column, Column]] = []
    for role in (t for t in g.table_link if g.table_link[t] > 0 and schema.is_role(t)):
        # 'employees who earn less than their manager': the same attribute of a record
        # and of the record in that role.
        base = schema.table(role).physical
        for column in schema.tables[role].columns:
            if (column.kind in (NUMBER, DATE) and not column.is_identifier) or (
                    schema.is_key(column.key) and not column.primary_key):
                pairs.append((schema.table(base).column(column.name), column))
    options: list[Option] = []
    pairs += [(a, b) for i, a in enumerate(linked) for b in linked[i + 1:]]
    pairs = [(a, b) for a, b in dict.fromkeys(pairs)
             if not any(fk.table == a.table and a.name in fk.columns and fk.ref_table == b.table
                        for fk in schema.foreign_keys)]          # not the reference itself
    pairs.sort(key=lambda p: -(g.column_link.get(p[0].key, 0) + g.column_link.get(p[1].key, 0)))
    for a, b in pairs:
        if schema.is_key(a.key):
            # 'in the same store as their manager': references compare for equality only.
            options += [Option(f"x{len(options)}", f"the {column_phrase(schema, a.key)} is {word} the "
                               f"{column_phrase(schema, b.key)}", Condition("columns", a.key, op, (b.key,)))
                        for op, word in (("=", "the same as"), ("!=", "different from"))]
            continue
        numeric = a.kind in (NUMBER, DATE) and b.kind == a.kind
        text = a.kind == TEXT and b.kind == TEXT
        if not (numeric or text) or a.key in schema.equivalents(b.key):
            continue
        paths = schema.shortest_paths(a.table, b.table, limit=1) if a.table != b.table else [[]]
        if not paths or len(paths[0]) > 2:
            continue
        pa, pb = column_phrase(schema, a.key), column_phrase(schema, b.key)
        ops = ((("<", "earlier than"), (">", "later than"), ("=", "the same as")) if a.kind == DATE
               else (("<", "less than"), (">", "greater than"), ("=", "equal to")) if numeric
               else (("=", "the same as"), ("!=", "different from")))
        for op, word in ops:
            options.append(Option(f"x{len(options)}", f"the {pa} is {word} the {pb}",
                                  Condition("columns", a.key, op, (b.key,))))
    return options[:60]


def _order_candidates(g: Grounding, cands: Candidates, columns: list[Column]) -> None:
    schema = g.schema
    orders: list[Option] = []

    def add(text: str, payload: Ordering) -> None:
        orders.append(Option(f"s{len(orders)}", text, payload))

    for table in _by_relevance(g, cands.tables):
        if schema.countable(table):
            label = entity(schema, table)
            add(f"by how many {label} records each result has, most first",
                Ordering("COUNT", None, table, True))
            add(f"by how many {label} records each result has, fewest first",
                Ordering("COUNT", None, table, False))
    for column in columns:
        if column.is_identifier and not column.primary_key:
            continue
        phrase = column_phrase(schema, column.key)
        if column.aggregated:
            add(f"by {phrase}, highest first", Ordering("EXPR", column.key, None, True))
            add(f"by {phrase}, lowest first", Ordering("EXPR", column.key, None, False))
            continue
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
    def relevance(option: Option) -> float:
        o = option.payload
        key = o.column or ""
        return g.column_link.get(key, 0) + g.table_link.get(o.table or key.split(".")[0], 0)

    position = {id(o): i for i, o in enumerate(orders)}
    orders = sorted(orders, key=lambda o: (-relevance(o), position[id(o)]))[:250]
    cands.orders = [Option(f"s{i}", o.text, o.payload) for i, o in enumerate(orders)]


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


_THAN = re.compile(r"\bthan\s+(?:that of\s+|those of\s+)?(?:some|any|all|every|each|the|a|an)?\s*"
                   r"(?:\w+\s+){0,7}$", re.I)


def _after_than(question: str, lit: Literal) -> bool:
    """'a higher resolution than English songs', 'longer than some drivers in race 841':
    a value describing the records compared against, a few words after 'than'."""
    before = question[max(0, lit.start - 60):lit.start]
    return bool(_THAN.search(before)) and not (lit.kind == "number" and re.search(r"\bthan\s*$", before, re.I))


SUBSET_ROLE = Option("subset", "it describes the other records that results are compared "
                                "against, not the results (e.g. 'more expensive than any wine from "
                                "2006', 'older than every engineer')", "subset")


def _literal_slots(g: Grounding, lit: Literal, tables: list[str], classes: list[str]) -> LiteralSlots:
    schema = g.schema
    rep_of = {m: rep for rep in classes for m in schema.equivalents(rep)}

    def scoped(key: str) -> str:
        return rep_of.get(key, key)

    columns: list[Option] = []
    if lit.kind == "period":
        first, end, label = lit.value
        keys = [k for k in classes if schema.column(k).kind == DATE and schema.column(k).formula is None]
        keys.sort(key=lambda k: (-g.column_link.get(k, 0), -_table_relevance(g, k.split(".")[0])))
        columns = [Option(f"c{i}", column_phrase(schema, k), (k, lit.value)) for i, k in enumerate(keys[:200])]
        roles = [Option("value", f"a time period ({label}) that a date of the results must fall in, "
                                 "before or after", "value"),
                 Option("other", "not a condition on a date", None)]
        return LiteralSlots(lit, roles, columns, dict(PERIOD_OPS), [], [], literal_context(g.question, lit))
    if lit.kind in ("text", "date"):
        stored: dict[str, Any] = {}
        for key, value in lit.alternatives:
            if key.split(".")[0] in tables:
                stored.setdefault(scoped(key), value)
        keys = list(stored)
        partial = lit.kind == "text" and bool(CONTAINS_CUE.search(g.question))
        if partial:
            keys += [k for k in classes if schema.column(k).kind == TEXT and not schema.is_key(k)
                     and k not in keys]
        if not keys:
            wanted = (DATE, TEXT) if lit.kind == "date" else (TEXT, DATE)
            keys = [k for k in classes if schema.column(k).kind in wanted
                    and not schema.column(k).is_identifier]
            keys.sort(key=lambda k: (lit.kind == "date" and schema.column(k).kind != DATE,
                                     -g.column_link.get(k, 0),
                                     -_table_relevance(g, k.split(".")[0])))
        for key in keys[:200]:
            value = stored.get(key, lit.value)
            columns.append(Option(f"c{len(columns)}",
                                  f"{_describe_column(schema, key, 2)} (value '{value}')", (key, value)))
        roles = [Option("value", "a value of an attribute that results are filtered by", "value")]
        if lit.kind == "text":
            roles.append(Option("reference", "it identifies one particular record, and results must "
                                             "share an attribute with that record (e.g. 'in the same "
                                             "department as Clara')", "reference"))
        if QUANTIFIED_CUE.search(g.question) or _after_than(g.question, lit):
            roles.append(SUBSET_ROLE)
        roles.append(Option("other", "not a filter value (e.g. part of a table or column name, "
                                     "or a word used in another sense)", None))
        if lit.kind == "date":
            ops = DATE_OPS
        else:
            ops = EXACT_TEXT_OPS if stored and not partial else PARTIAL_TEXT_OPS
    else:
        matched = {scoped(c) for c in lit.columns}
        numeric = [k for k in classes if schema.column(k).kind in (NUMBER, DATE) or k in matched]
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
                 *([SUBSET_ROLE] if QUANTIFIED_CUE.search(g.question) or _after_than(g.question, lit)
                   else []),
                 Option("other", "none of these (e.g. part of a name, an identifier inside text, "
                                 "or a column name such as '200 meters')", None)]
        ops = NUMERIC_OPS
    count_tables = [Option(f"k{i}", f"how many {entity(schema, t)} records", t)
                    for i, t in enumerate(t for t in _by_relevance(g, tables) if schema.countable(t))]
    group_funcs = [Option(f, w, f) for f, w in AGG_FUNCS]
    return LiteralSlots(lit, roles, columns, dict(ops), count_tables, group_funcs,
                        literal_context(g.question, lit))
