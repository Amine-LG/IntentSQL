"""Natural language -> Intent, using Jev only for bounded, grounded decisions.

Round 1 is one speculative fan-out: every decision the request might need is
asked at once, over candidates that code generates from the schema and the
request's grounded literals.  Alternatives that exclude each other compete in a
single Choice (never as independent yes/no questions, which over-select
near-synonyms).  Dependent decisions are asked speculatively for every parent
option, and only the branch selected by the parent is consumed.

Code assembles the answers into an ``Intent``.  Join paths, grouping keys,
subquery shapes and FK-equivalent column choices are derived deterministically.
When assembly finds a gap (e.g. nothing to return), it issues a targeted
follow-up round instead of guessing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from intentsql.grounding import Grounding, Literal
from intentsql.intent import Condition, Intent, Ordering, OutputItem, _negate, column_phrase, entity
from intentsql.jev import Answer, Question, choice, noul
from intentsql.schema import BOOLEAN, DATE, NUMBER, TEXT, Column, Schema, flag_values
from intentsql.text import ORDINALS, lemma

NUMERIC_OPS = {"=": "equal to", ">": "greater than (more, over, above, older, larger)",
               "<": "less than (fewer, under, below, younger, smaller)",
               ">=": "at least (or more, no less than)", "<=": "at most (or less, no more than)",
               "!=": "not equal to (the request negates it)"}
DATE_OPS = {"=": "on exactly this date", ">": "after this date", "<": "before this date",
            ">=": "on or after this date", "<=": "on or before this date",
            "!=": "not on this date"}
PERIOD_OPS = {"within": "during this period", "<": "before this period", ">": "after this period"}
EXACT_TEXT_OPS = {"=": "results have exactly this value",
                  "!=": "results must NOT have this value (the request negates it: 'not', 'other than')"}
PARTIAL_TEXT_OPS = {"contains": "the attribute contains this text somewhere (e.g. 'with X in the name', 'about X')",
                    "=": "the attribute is exactly this value",
                    "starts": "the attribute starts with this text",
                    "ends": "the attribute ends with this text",
                    "!=": "the attribute must NOT be this value",
                    "not_contains": "the attribute must NOT contain this text"}
AGG_FUNCS = (("AVG", "average"), ("SUM", "total"), ("MAX", "maximum"), ("MIN", "minimum"))
EXPLICIT_AGG = {
    "AVG": re.compile(r"\b(average|mean|avg)\b", re.I),
    "SUM": re.compile(r"\b(total|sum)\b", re.I),
    "MAX": re.compile(r"\b(max|maximum|maxium|maximal)\b", re.I),
    "MIN": re.compile(r"\b(min|min[iu]mum|mininum|minimal)\b", re.I),
}
COUNT_CUE = re.compile(r"\b(number of|how many|count of|count the)\b", re.I)
STAR_CUE = re.compile(r"\b(all (the )?(information|info|details|data|attributes|columns|fields)|"
                      r"everything|every (detail|attribute|column))\b", re.I)
AGG_WORDS = {
    "AVG": EXPLICIT_AGG["AVG"], "SUM": EXPLICIT_AGG["SUM"],
    "MAX": re.compile(r"\b(max|maximum|maxium|maximal|highest|largest|biggest|greatest|longest)\b", re.I),
    "MIN": re.compile(r"\b(min|min[iu]mum|mininum|minimal|lowest|smallest|least|shortest)\b", re.I),
}
COUNT_KINDS_CUE = re.compile(r"\bhow many (kinds|types|sorts|varieties)\b|\bnumber of (kinds|types)\b", re.I)
CONTAINS_CUE = re.compile(r"\b(contain\w*|includ\w*|with the (word|letter|text|string)|start\w* with|"
                          r"begin\w* with|end\w* with|in (its|their) (name|title))\b", re.I)
DISTINCT_CUE = re.compile(r"\b(distinct|different|unique|differ\w*|various)\b", re.I)
NEGATION_CUE = re.compile(r"\b(not|no|never|without|none|except|excluding|neither|nor|other than|"
                          r"lacks?|apart from|besides)\b|n't\b", re.I)
AGG_COMPARE_CUE = re.compile(r"\b(average|mean|(than|above|below|over|under|exceed\w*|beyond) (any|all|every|"
                             r"each|the (maximum|minimum|largest|smallest|highest|lowest|max|min))|"
                             r"than (that|those) of)\b", re.I)
COMPARATIVE_CUE = re.compile(r"\b(than|above|below|over|under|exceed\w*|beyond|more|less|fewer|higher|lower|"
                             r"greater|smaller|bigger|larger|at least|at most|outperform\w*)\b", re.I)
# 'older than every engineer', 'above any wine from 2006': the literal describes the
# records the aggregate is computed over, not the results.
QUANTIFIED_CUE = re.compile(r"\b(than|above|below|over|under|exceed\w*|beyond) (any|all|every|each)\b", re.I)
ALWAYS_CUE = re.compile(r"\b(always|every time|only ever|at all times|consistently|(?:has|have|had) only|"
                        r"only (?:has|have|had|contain\w*|sell\w*|stock\w*|include\w*|offer\w*)|"
                        r"all (?:of )?(?:its|their) \w+ (?:are|were|have|had))\b", re.I)
BOTH_CUE = re.compile(r"\bboth\b|\bnot only\b.+\bbut also\b|\bas well as\b|\band also\b", re.I)
THEN_CUE = re.compile(r"\bthen\b|\bfollowed by\b|\bsecondarily\b|\band (then )?by\b|"
                      r"\b(order|sort)\w* by \w+(?: \w+){0,4} and\b", re.I)
EXCEPT_CUE = re.compile(r"\b(except|excluding|never|without|other than|apart from|besides|"
                        r"(?:do|does|did|have|has|had)(?:n't| not) (?:ever )?\w+|"
                        r"(?:don't|doesn't|didn't|haven't|hasn't|hadn't) \w+)\b", re.I)
TOP_GROUP_CUE = re.compile(r"\b(most|least)\s+(common|popular|frequent|usual)\b", re.I)
NULL_CUE = re.compile(r"\b(without|missing|unknown|empty|null|blank|lacks?|lacking|not (?:set|given|"
                      r"recorded|specified|available|assigned|provided|known|listed)|(?:do|does|did)(?: not|n't) "
                      r"have|has no|have no|with no|no \w+ (?:assigned|recorded|given|set|provided|listed)|"
                      r"(?:has|have|with) (?:a|an|any) \w+ (?:recorded|listed|set|given|on file))\b", re.I)
OWN_GROUP_CUE = re.compile(r"\b(their|its|his|her|own|same|respective|within|that of)\b", re.I)
PER_GROUP_SUPERLATIVE = re.compile(r"\b(\w+est|most \w+|least \w+|highest|lowest|top)\b.*\b(in|of|for|within) "
                                   r"(each|every|its|their)\b|\b(each|every|per)\b.*\b(\w+est|highest|lowest)\b",
                                   re.I)
COLUMN_COMPARE_CUE = re.compile(r"\b(than|same|equal\w*|identical|match\w*|differ\w*|exceed\w*|before|after|"
                                r"earlier|later|older|younger|"
                                r"as (?:high|low|much|many|large|big|long) as)\b", re.I)
PCT_CUE = re.compile(r"\b(percent\w*|proportion|share|fraction)\b|%", re.I)
KEEP_EMPTY_CUE = re.compile(r"\b(including|include|even) (?:those|the ones|\w+ )?(?:with|that have|having|"
                            r"without) (?:no|zero|none|any)\b|\b(?:with|including|show(?:ing)?) zeros?\b|"
                            r"\beven (?:if|when|those with) (?:they|it)? ?(?:have|has)? ?(?:none|no|zero)\b", re.I)
EACH_CUE = re.compile(r"\b(each|every|per|within)\b", re.I)
RANGE_CUE = re.compile(r"\bbetween\b|\bfrom\s+\S+\s+(to|through|until|till)\b", re.I)
CATEGORICAL_MAX = 12
MAX_ROUNDS = 2          # follow-up rounds after the main fan-out


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


# ---- questions ---------------------------------------------------------------------------

_LEAD = re.compile(
    r"^(?:(?:please|kindly|can you|could you|would you|i (?:want|need|would like) to (?:know|see))[,\s]+)*"
    r"(?:(?:what|which|who|whom)\s+(?:is|are|was|were|'s)\s+|"
    r"(?:show|list|find|give|return|display|tell|get|print|report|retrieve|select|identify|provide|output)"
    r"\s+(?:me\s+|us\s+)?(?:all\s+)?|(?:what|which|who|whose)\s+)", re.I)
_BOUNDARY = re.compile(
    r"\b(who|that|which|whose|where|when|whom|with|without|having|has|have|had|from|in|on|at|for|"
    r"ordered|sorted|order|sort|by|than|between|if|after|before|during|whereas|located|born|is|are|"
    r"was|were|do|does|did)\b|[?.;,]\s*(?:and\s+)?(?:sort|order|list|show)", re.I)


def asked_for(question: str) -> str:
    """The part of a request naming what to return ('the names and ages of singers')."""
    text = question.strip()
    match = _LEAD.match(text)
    rest = text[match.end():] if match else text
    words = rest.split()
    # Skip a leading article/quantifier, then cut at the first clause boundary
    # that follows at least one content word.
    cut = len(rest)
    for boundary in _BOUNDARY.finditer(rest):
        if boundary.start() > 0 and rest[:boundary.start()].strip():
            cut = boundary.start()
            break
    phrase = re.sub(r"\s+(and|or|then)$", "", rest[:cut].strip(" ,?."), flags=re.I)
    return phrase if phrase and len(phrase) < len(text) and words else question


def _state(g: Grounding, cands: Candidates, strategy: Strategy) -> dict[str, Any]:
    schema = g.schema
    state: dict[str, Any] = {"request": g.question}
    if strategy.output_phrase:
        state["asked_for"] = asked_for(g.question)
    if strategy.include_schema_in_state:
        def label(c: Column) -> str:
            values = schema.values.get(c.key, ())
            if strategy.state_values and values and c.kind == TEXT and not c.is_identifier:
                return f"{c.label} (e.g. {', '.join(repr(v) for v in values[:2])})"
            return c.label
        state["database"] = {
            entity(schema, t): (f"another {entity(schema, schema.tables[t].physical)} (same attributes)"
                                if schema.is_role(t) else [label(c) for c in schema.tables[t].columns])
            for t in cands.tables}
    if strategy.state_relations:
        state["relationships"] = [
            f"{entity(schema, fk.table)}.{schema.table(fk.table).column(fk.columns[0]).label} "
            f"refers to {entity(schema, fk.ref_table)}"
            for fk in schema.foreign_keys
            if fk.table in cands.tables and fk.ref_table in cands.tables]
    return state


def round_one(g: Grounding, cands: Candidates, strategy: Strategy) -> dict[str, Question]:
    schema = g.schema
    q: dict[str, Question] = {}
    q["count"] = choice(
        "Does `request` ask to be told a count (how many, the number of)? If so, a count of what? "
        "Ranking or filtering by a count ('the most', 'at least 3') does not by itself ask to show "
        "the count, and 'what are the different X' asks to list them, not to count them.",
        {**{o.key: o.text for o in cands.counts}, "none": "the request does not ask for a count"})
    for group in cands.outputs:
        opts = {o.key: o.text for o in group.options}
        opts["no"] = (f"not shown: the {group.phrase} is not requested, or is only used to "
                      "filter, sort, rank or group the results")
        hint = (" An identifier or code is shown only when the request asks for the id, code or "
                "number itself, not when it merely mentions the record."
                if schema.is_key(group.options[0].payload.column) else "")
        if strategy.output_phrase:
            q[f"out_{group.key}"] = choice(
                f"Is the {group.phrase} one of the things named in `asked_for` (the part of `request` "
                "that says what to return)? If so, in which form?" + hint, opts)
        else:
            q[f"out_{group.key}"] = choice(
                f"Does `request` ask to show the {group.phrase} in the results? If so, in which form?"
                + hint, opts)
    if cands.stars:
        q["star"] = choice(
            "Does `request` ask for all information (every attribute) of some records? If so, "
            "of which records?",
            {**{o.key: o.text for o in cands.stars}, "none": "no, it asks for specific attributes"})
    for option in cands.multi_count:
        q[f"cnt2_{option.key}"] = noul(f"Does `request` ask for {option.text}?")
    for key, func in cands.multi_agg:
        word = dict(AGG_FUNCS)[func]
        q[f"agg2_{key}_{func}"] = noul(
            f"Does `request` ask to show the {word} {column_phrase(schema, key)}?")
    negation = bool(NEGATION_CUE.search(g.question))
    for lit_id, slots in cands.literals.items():
        lit = slots.literal
        context = slots.context
        q[f"role_{lit_id}"] = choice(
            {"value": context, "question": "In `request`, what is the bracketed part of `value`?"},
            {o.key: o.text for o in slots.roles})
        if len(slots.columns) >= 2:
            q[f"lcol_{lit_id}"] = choice(
                {"value": context,
                 "question": "Which attribute does `request` compare with the bracketed part of `value`?"},
                {o.key: o.text for o in slots.columns})
        q[f"lop_{lit_id}"] = choice(
            {"value": context,
             "question": "How does `request` compare the attribute or count with the bracketed "
                         "part of `value`?"},
            slots.ops)
        if lit.kind == "number" and len(slots.count_tables) >= 2:
            q[f"lcnt_{lit_id}"] = choice(
                {"value": context, "question": "If the bracketed part of `value` limits how many "
                                               "related records each result has, records of which "
                                               "kind are counted?"},
                {o.key: o.text for o in slots.count_tables})
        if lit.kind == "number":
            q[f"lfun_{lit_id}"] = choice(
                {"value": context, "question": "If the bracketed part of `value` is a threshold on a "
                                               "per-group total or average, which aggregate is it?"},
                {o.key: o.text for o in slots.group_funcs})
        if lit.kind == "text" and len(cands.shares) >= 2:
            q[f"lshr_{lit_id}"] = choice(
                {"value": context, "question": "If the bracketed part of `value` identifies a record "
                                               "that results must resemble, which attribute must "
                                               "they share with it?"},
                {o.key: o.text for o in cands.shares})
        if ALWAYS_CUE.search(g.question):
            q[f"lall_{lit_id}"] = noul(
                {"value": context,
                 "question": "Does `request` require that every record of each result matches the "
                             "bracketed part of `value` (e.g. 'whose temperature was always below 70')?"})
        if negation:
            q[f"lexc_{lit_id}"] = noul(
                {"value": context,
                 "question": "Does `request` ask to exclude the results that match the bracketed "
                             "part of `value` (e.g. 'all X except those ...', 'X that never ...')?"})
    for key, options in cands.categorical.items():
        phrase = column_phrase(schema, key)
        opts = {o.key: o.text for o in options}
        opts["none"] = f"no restriction on {phrase}"
        if flag_values(schema.values.get(key, ())):
            label = schema.column(key).label
            opts["none"] = f"neither: being {label} or not is not part of `request`"
            q[f"cat_{key}"] = choice(
                f"Does `request` say whether the {entity(schema, schema.column(key).table)} records "
                f"involved are {label}? Choose an option only when the request says so.", opts)
            continue
        q[f"cat_{key}"] = choice(
            f"Does `request` itself restrict the results to one particular {phrase}? "
            "Choose a value only when the request states that restriction.", opts)
    for key, subject, target in cands.relations:
        s_label, t_label = entity(schema, subject), entity(schema, target)
        q[f"rel_{key}"] = choice(
            f"Does `request` restrict {s_label} records by whether they have related {t_label} records?",
            {"none": "no such restriction",
             "has": f"only {s_label} records that have at least one related {t_label} record",
             "lacks": f"only {s_label} records that have no related {t_label} record at all"})
    if cands.vs_aggregate:
        q["vsagg"] = choice(
            "Does `request` compare each result's value with an average, maximum or minimum "
            "computed over all records, or with the most/least common value? If so, which?",
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
    if THEN_CUE.search(g.question):
        q["order2"] = choice(
            "If `request` sorts results by a second criterion after the first one (e.g. 'then "
            "by name'), what is that second criterion?",
            {**{o.key: o.text for o in cands.orders}, "unordered": "no second sort criterion"})
    q["limit"] = choice(
        "Does `request` ask for only the top or first item(s) of a ranked list? If so, how many?",
        {o.key: o.text for o in cands.limits})
    q["scope"] = choice(
        "What kind of request is `request`?",
        {"read": "a question answered by looking up, listing, counting, summarizing or comparing data "
                 "stored in `database`",
         "modify": "a request to add, change, update or delete data",
         "other": "something else: a prediction, an opinion, general knowledge, or information that "
                  "`database` does not store"})
    if cands.nulls:
        q["nulls"] = choice(
            "Does `request` restrict results to records where some attribute is missing (empty, "
            "unknown, not recorded) or present? If so, which?",
            {**{o.key: o.text for o in cands.nulls}, "none": "no such restriction"})
    if cands.col_compares:
        q["colcmp"] = choice(
            "Does `request` compare two attributes of each result with each other (not with a "
            "fixed value)? If so, which comparison?",
            {**{o.key: o.text for o in cands.col_compares}, "none": "no such comparison"})
    q["connector"] = choice(
        "If `request` states several conditions, must every result satisfy all of them, or "
        "is satisfying any one of them enough?",
        {"all": "all conditions must hold together (and)",
         "any": "satisfying any one condition is enough (or / either)"})
    for pair, options in cands.joins.items():
        a, b = pair.split("|")
        q[f"join_{pair}"] = choice(
            f"If `request` relates {entity(schema, a)} and {entity(schema, b)}, which "
            "relationship does it mean?", {o.key: o.text for o in options})
    return q


def missing_output_question(cands: Candidates) -> dict[str, Question]:
    options = {o.key: o.text for o in cands.counts}
    for group in cands.outputs:
        options[f"{group.key}:value"] = group.options[0].text
    options = dict(list(options.items())[:255])
    return {"main_output": choice("What does `request` ask to be shown? Pick the main item.", options)}


# ---- assembly -------------------------------------------------------------------------------

@dataclass
class Interpretation:
    intent: Intent | None
    decisions: list[Decision]
    uncertain: list[str]
    message: str | None = None
    followup: dict[str, Question] | None = None
    answers: dict[str, Answer] = field(default_factory=dict)


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

    scope = answers.get("scope")
    if scope is not None:
        record("scope", "request scope", "kind of request", scope, scope.choice)
        if scope.choice == "modify" and scope.top_probability >= 0.6:
            return Interpretation(None, record.decisions, record.uncertain,
                                  "IntentSQL only reads data; it does not add, change or delete records.")
        if scope.choice == "other" and scope.top_probability >= 0.85:
            return Interpretation(None, record.decisions, record.uncertain,
                                  "This request does not look answerable from the data in this database.")

    # -- outputs
    outputs: list[OutputItem] = []
    count_answer = answers["count"]
    if count_answer.choice != "none":
        option = _pick(cands.counts, count_answer)
        outputs.append(option.payload)
        record("count", "output", "count", count_answer, option.text)
    if "star" in answers and answers["star"].choice != "none":
        option = _pick(cands.stars, answers["star"])
        outputs.append(option.payload)
        record("star", "output", option.text, answers["star"], option.text)
    for option in cands.multi_count:
        answer = answers[f"cnt2_{option.key}"]
        if (answer.p or 0) >= 0.6 and option.payload not in outputs:
            outputs.append(option.payload)
            record(f"cnt2_{option.key}", "output", option.text, answer, True)
    for group in cands.outputs:
        answer = answers[f"out_{group.key}"]
        if answer.choice == "no" or any(o.star for o in outputs):
            continue
        option = _pick(group.options, answer)
        outputs.append(option.payload)
        record(f"out_{group.key}", "output", group.phrase, answer, option.key)
    extra: list[OutputItem] = []
    for key, func in cands.multi_agg:
        answer = answers[f"agg2_{key}_{func}"]
        item = OutputItem(func, key)
        if (answer.p or 0) >= 0.6 and item not in outputs:
            extra.append(item)
            record(f"agg2_{key}_{func}", "output", f"{func} {key}", answer, True)
    for item in extra:
        if any(o.column == item.column and o.func and o != item for o in outputs + extra):
            outputs = [o for o in outputs if not (o.column == item.column and o.func is None)]
            outputs.append(item)
    if not outputs:
        if "main_output" not in answers:
            return Interpretation(None, record.decisions, record.uncertain,
                                  followup=missing_output_question(cands))
        key = answers["main_output"].picked
        record("main_output", "output", "main output", answers["main_output"], key)
        if key.endswith(":value"):
            group = next(o for o in cands.outputs if o.key == key.split(":")[0])
            outputs.append(group.options[0].payload)
        else:
            outputs.append(_pick(cands.counts, answers["main_output"]).payload)

    # -- literal-bound conditions
    conditions: list[Condition] = []
    excluded: list[Condition] = []
    subset: list[Condition] = []         # conditions on the records an aggregate is computed over
    literal_conditions: list[tuple[Literal, Condition]] = []
    denied_flags: list[tuple[Condition, Condition]] = []
    limit: int | None = None
    for lit_id, slots in cands.literals.items():
        lit = slots.literal
        role_answer = answers[f"role_{lit_id}"]
        role = _pick(slots.roles, role_answer).payload
        record(f"role_{lit_id}", "literal role", f"role of {slots.context}", role_answer, role,
               {o.key: o.text for o in slots.roles})
        if role is None:
            continue
        op = answers[f"lop_{lit_id}"].picked
        if role == "limit":
            if isinstance(lit.value, int):
                limit = lit.value
            continue
        exclude = ((answers[f"lexc_{lit_id}"].p or 0) >= strategy.exclusion_threshold
                   if f"lexc_{lit_id}" in answers else False)
        if exclude:
            record(f"lexc_{lit_id}", "exclusion", f"exclude {slots.context}",
                   answers[f"lexc_{lit_id}"], True)
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
            func = answers[f"lfun_{lit_id}"].picked
            conditions.append(Condition("having", key, _numeric_op(op), (value,), func=func))
            continue
        record(f"lop_{lit_id}", "literal operator", f"operator for {slots.context}",
               answers[f"lop_{lit_id}"], op)
        if lit.kind == "period":
            first, end, label = value
            conditions.append(Condition("period", key, values=(first, end, label)) if op == "within" else
                              Condition("compare", key, "<", (first,)) if op == "<" else
                              Condition("compare", key, ">=", (end,)))
            continue
        if lit.kind == "text":
            kind = "text" if op in ("contains", "starts", "ends", "not_contains") else "compare"
            condition = Condition(kind, key, op, (value,))
        else:
            condition = Condition("compare", key, _numeric_op(op), (value,))
        if role == "subset":
            subset.append(condition)
            continue
        if role == "reference" and f"lshr_{lit_id}" in answers:
            shared = _pick(cands.shares, answers[f"lshr_{lit_id}"]).payload
            record(f"lshr_{lit_id}", "shared attribute", f"shared with {slots.context}",
                   answers[f"lshr_{lit_id}"], shared)
            if shared not in schema.equivalents(key):
                conditions.append(Condition("same_as", shared, inner=(condition,)))
                continue
        always = (answers[f"lall_{lit_id}"].p or 0) >= 0.6 if f"lall_{lit_id}" in answers else False
        negative = condition.op in ("!=", "not_contains")
        if always and not negative and not _identifies_rows(schema, key.split(".")[0], outputs):
            # 'zip codes whose dew point was always below 70': no record may violate it.
            record(f"lall_{lit_id}", "universal condition", f"always {slots.context}",
                   answers[f"lall_{lit_id}"], True)
            excluded.append(_negate(condition))
        elif exclude and not negative:
            excluded.append(condition)
        elif (exclude or negative) and EXCEPT_CUE.search(g.question) and not _identifies_rows(
                schema, key.split(".")[0], outputs):
            # 'publishers which do not have a book in 1989': a publisher qualifies when none
            # of its rows matches -- a set difference, not a row filter.
            excluded.append(_positive(condition))
        else:
            conditions.append(condition)
        literal_conditions.append((lit, condition))
    conditions, excluded = _but_not(g, conditions, excluded, literal_conditions)
    conditions = _join_name_parts(g, conditions, literal_conditions)
    excluded = _join_name_parts(g, excluded, literal_conditions)

    # -- implicit categorical values (unless a literal already restricts that column)
    restricted = {schema.canonical(c.column) for c in conditions + excluded + subset if c.column}
    for key, options in cands.categorical.items():
        answer = _stated_flag(g, key, options, answers[f"cat_{key}"])
        if answer.choice == "none" or answer.top_probability < strategy.categorical_threshold:
            continue
        if schema.canonical(key) in restricted:
            continue
        option = _pick(options, answer)
        record(f"cat_{key}", "implicit value", option.text, answer, option.payload)
        condition = Condition("compare", key, "=", (option.payload,))
        conditions.append(condition)
        flag = flag_values(schema.values.get(key, ()))
        word = schema.column(key).label.split()[0]
        if flag and option.payload == flag[1] and re.search(
                rf"\b(no|without|zero|never \w+(?: any)?)\s+(?:any\s+)?{re.escape(word)}", g.question, re.I):
            denied_flags.append((condition, Condition("compare", key, "=", (flag[0],))))

    # -- missing / present values
    if "nulls" in answers and answers["nulls"].choice != "none":
        option = _pick(cands.nulls, answers["nulls"])
        record("nulls", "missing values", option.text, answers["nulls"], option.text)
        conditions.append(option.payload)

    if "colcmp" in answers and answers["colcmp"].choice != "none":
        option = _pick(cands.col_compares, answers["colcmp"])
        record("colcmp", "column comparison", option.text, answers["colcmp"], option.text)
        conditions.append(option.payload)

    # -- comparison with an aggregate
    top_group: Condition | None = None
    if "vsagg" in answers and answers["vsagg"].choice != "none":
        option = _pick(cands.vs_aggregate, answers["vsagg"])
        record("vsagg", "aggregate comparison", option.text, answers["vsagg"], option.text)
        if option.payload.kind == "top_group":
            top_group = option.payload      # kept unless a count ranking already expresses it
        elif option.payload.kind == "vs_aggregate" and subset:
            conditions.append(replace(option.payload, inner=tuple(subset)))
            subset = []
        else:
            conditions.append(option.payload)
    conditions.extend(subset)               # no aggregate to attach them to: plain conditions

    # -- grouping, ordering, limit, distinct, connector, relationship roles
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
    if order and "order2" in answers and answers["order2"].choice != "unordered":
        option = _pick(cands.orders, answers["order2"])
        if option.payload.column != order[0].column or option.payload.table != order[0].table:
            order = order + (option.payload,)
            record("order2", "ordering", option.text, answers["order2"], option.text)
    limit_option = _pick(cands.limits, answers["limit"])
    record("limit", "limit", limit_option.text, answers["limit"], limit_option.payload)
    if limit_option.payload is not None:
        limit = limit_option.payload
    if not order:
        limit = None
    if limit == 1 and order and not order[0].is_aggregate and any(
            c.kind == "vs_group" and c.op == "=" and c.func in ("MAX", "MIN") for c in conditions):
        order, limit = (), None     # 'the most expensive product in each category': one per group
    if top_group is not None and not (order and order[0].is_aggregate):
        conditions.append(top_group)
    # 'distinct / different / unique' lists each value once (Spider train: ~95% of
    # such listings); without the cue, rows are listed as stored.
    distinct = bool(DISTINCT_CUE.search(g.question))
    connector = "OR" if answers["connector"].choice == "any" else "AND"
    join_choices = []
    for pair, options in cands.joins.items():
        answer = answers[f"join_{pair}"]
        option = _pick(options, answer)
        join_choices.append((pair, option.payload))
        record(f"join_{pair}", "relationship role", option.text, answer, option.payload)

    # -- deterministic shaping
    if order and limit is not None:
        multi = {(k, f) for k, f in cands.multi_agg}
        outputs = [o for o in outputs if not ((o.column, o.func) in multi and o.func in ("MIN", "MAX")
                                              and len([x for x in outputs if x.column == o.column]) > 1)]
    if outputs and all(o.is_aggregate for o in outputs) and not group_by and not any(
            c.kind in ("having", "having_vs") for c in conditions):
        order, limit = (), None    # a single aggregate row has nothing to sort or cut
    outputs = _prefer_named_tables(g, outputs)
    outputs = _shape_outputs(g, outputs, order, limit, group_by, conditions)
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
            # A table the request names outright ('patients who have made appointments')
            # needs less certainty than one only implied.
            named = g.table_named.get(target, 0) >= 1.0
            if answer.top_probability < (0.5 if named else strategy.related_threshold):
                continue
            record(f"rel_{key}", "relationship",
                   f"{entity(schema, rel_subject)} that has a {entity(schema, target)}", answer, "has")
            conditions.append(Condition("related", None, table=target))
        else:
            if excluded:
                continue        # the exclusion already says which related records are absent
            record(f"rel_{key}", "relationship",
                   f"{entity(schema, rel_subject)} that has no {entity(schema, target)}", answer, "lacks")
            far = _far_side(schema, rel_subject, target)
            inner = [c for c in conditions if c.column and c.column.split(".")[0] in far
                     and c.column.split(".")[0] not in output_tables]
            conditions = [c for c in conditions if c not in inner]
            # The negation lives in the relationship ('without any goalies'): conditions
            # describing the excluded related records keep positive polarity.
            inner = [_positive(c) for c in inner]
            conditions.append(Condition("absent", None, table=target, inner=tuple(inner)))
            subject = rel_subject

    for condition, positive in denied_flags:
        # 'suppliers with no discontinued products': no related product is discontinued
        # (not 'has a product that is not discontinued').
        table = condition.column.split(".")[0] if condition.column else None
        if condition in conditions and subject and table and table != subject and schema.one_to_many(subject, table):
            conditions = [c for c in conditions if c is not condition]
            conditions.append(Condition("absent", None, table=table, inner=(positive,)))
    if subject:
        # 'the names of gymnasts': gymnast rows are people rows; naming the subtype restricts.
        used = output_tables | {t for c in conditions for t in c.tables} | {
            c.table for c in conditions if c.table}
        for table, link in g.table_named.items():
            if link >= 1.0 and table not in used and schema.is_subtype(table, subject):
                conditions.append(Condition("related", None, table=table))
    conditions = _anti_joins(schema, conditions, subject)
    conditions = _dedupe_absent(schema, conditions, answers, cands, excluded)
    conditions = _drop_redundant_absent(schema, conditions, subject)
    explicit = {(c.column, c.func) for c in conditions if c.kind == "having" and c.column}
    conditions = [c for c in conditions if not (c.kind == "having_vs" and (c.column, c.func) in explicit)]
    conditions, connector, both = _normalize_conditions(g, conditions, connector, subject, outputs)
    group_keys = group_by or [o.column for o in outputs if o.column and not o.is_aggregate]
    order, conditions, outputs = _retarget_self_counts(g, cands, answers, subject, order,
                                                       conditions, group_keys, outputs)
    referenced = {t for o in outputs for t in o.tables} | {t for c in conditions for t in c.tables}
    referenced |= {o.table for o in order if o.table} | {o.column.split(".")[0] for o in order if o.column}
    reachable = set(referenced)
    if subject:
        for table in list(referenced):
            for path in schema.shortest_paths(subject, table, limit=1):
                reachable.update(step.table for step in path)
    conditions = [c for c in conditions if not (c.kind == "related" and c.table in reachable)]
    aggregated = (any(o.is_aggregate for o in outputs) or any(o.is_aggregate for o in order)
                  or any(c.kind in ("having", "having_vs") for c in conditions))
    limit_per: str | None = None
    if (not aggregated and group_by and order and not order[0].is_aggregate and limit and limit > 1
            and EACH_CUE.search(g.question)):
        limit_per = group_by[0]  # 'the two cheapest products in each category': rank within groups
        conditions = [c for c in conditions if not (c.kind == "vs_group" and c.op == "=")]
    if not aggregated and group_by:
        group_by = []          # "the name of each program" lists rows; it does not group them
        if limit_per is None:
            record.decisions = [d for d in record.decisions if d.key != "group"]
    if group_by and any(c.kind == "compare" and c.op == "=" and c.column in schema.equivalents(group_by[0])
                        for c in conditions):
        group_by = []          # grouping by a column pinned to one value yields a single group
    if group_by:
        # 'for each rank and gender': every plain categorical output is a group key too.
        key_identifies = _identifies_rows(schema, group_by[0].split(".")[0],
                                          [OutputItem(None, group_by[0])])
        for o in outputs:
            if key_identifies:
                break
            if (o.column and not o.is_aggregate and o.column not in group_by
                    and not schema.is_key(o.column)
                    and not any(o.column in schema.equivalents(k) for k in group_by)
                    and not _identifies_rows(schema, o.column.split(".")[0], [o])
                    and _categorical(schema, o.column)
                    and o.column.split(".")[0] == group_by[0].split(".")[0]):
                group_by.append(o.column)
        outputs = [o for o in outputs if not _ungrouped(schema, o, group_by)]
        group_classes = set(schema.equivalents(group_by[0]))
        outputs = [OutputItem("COUNT", None, o.column.split(".")[0])
                   if o.func == "COUNT" and o.distinct and o.column in group_classes else o
                   for o in outputs]
        if not any(o.column in group_classes for o in outputs if o.column and not o.is_aggregate):
            label = _group_label(schema, group_by[0], outputs, {t for c in conditions for t in c.tables})
            if label:
                outputs.insert(0, OutputItem(None, label))
        outputs = [o for i, o in enumerate(outputs) if o not in outputs[:i]]
    offset = _ordinal_offset(g.question) if order and limit == 1 else None
    if (order and limit == 1 and offset is None and not group_by and not order[0].is_aggregate
            and order[0].column and subject and order[0].column.split(".")[0] != subject
            and schema.one_to_many(order[0].column.split(".")[0], subject)):
        # 'scientists working on the project with the most hours': the superlative picks
        # a related record; every result linked to it qualifies (not just one row).
        conditions.append(Condition("vs_aggregate", order[0].column, "=",
                                    func="MAX" if order[0].desc else "MIN"))
        order, limit = (), None
    if any(o.is_aggregate for o in outputs) and not group_by and not any(
            not o.is_aggregate for o in outputs):
        distinct = False

    followup: dict[str, Question] = {}
    outputs = _resolve_count_sum(schema, outputs, group_by, answers, followup, record)
    outputs, conditions = _percentage(g, outputs, conditions, subject, answers, followup, record)
    keep_empty = bool(KEEP_EMPTY_CUE.search(g.question) and group_by and any(
        o.func == "COUNT" and o.column is None for o in outputs))
    if keep_empty:
        # 'including customers with no orders': the groups come from the grouped entity.
        canon = schema.canonical(group_by[0])
        subject = canon.split(".")[0] if schema.column(canon).primary_key else subject
        group_by = [canon]
        if not any(o.column and o.column.split(".")[0] == subject and not o.is_aggregate for o in outputs):
            label = _group_label(schema, canon, [], set())
            outputs = [OutputItem(None, label)] + [o for o in outputs if o.is_aggregate] if label else outputs
    intent = Intent(outputs=tuple(outputs), conditions=tuple(conditions), connector=connector,
                    group_by=tuple(group_by), order=order, limit=limit, offset=offset,
                    distinct=distinct and not group_by, both=both, subject=subject,
                    excluded=tuple(excluded), join_choices=tuple(join_choices),
                    keep_empty=keep_empty, limit_per=limit_per)
    intent = rebind_equivalents(schema, intent)
    intent = _choose_routes(g, intent, answers, followup, record)
    return Interpretation(intent, _effective_decisions(schema, cands, intent, record.decisions),
                          record.uncertain, followup=followup or None)


def _effective_decisions(schema: Schema, cands: Candidates, intent: Intent,
                         decisions: list[Decision]) -> list[Decision]:
    """Drop output decisions that later deterministic shaping removed from the plan, so
    the trace shown to users describes the answer they get."""
    shown = {k for o in intent.outputs if o.column for k in schema.equivalents(o.column)}
    groups = {g.key: g for g in cands.outputs}
    kept = []
    for d in decisions:
        group = groups.get(d.key.removeprefix("out_")) if d.key.startswith("out_") else None
        if group is not None and not any(o.payload.column in shown for o in group.options if o.payload.column):
            continue
        kept.append(d)
    return kept


def _anti_joins(schema: Schema, conditions: list[Condition], subject: str | None) -> list[Condition]:
    """'movies not reviewed by Chris Jackson' is not 'reviewer != Chris Jackson': a
    negated condition on records related one-to-many is an anti-join (NOT IN)."""
    if subject is None:
        return conditions
    out = []
    for c in conditions:
        table = c.column.split(".")[0] if c.column else None
        if (c.kind in ("compare", "text") and c.op in ("!=", "not_contains") and table
                and table != subject and schema.one_to_many(subject, table)):
            out.append(Condition("absent", None, table=table, inner=(_positive(c),)))
        else:
            out.append(c)
    return out


def _stated_flag(g: Grounding, key: str, options: list[Option], answer: Answer) -> Answer:
    """A flag the request states as a predicate ('products that are discontinued',
    'discontinued products') restricts the results even when Jev hesitates between the
    flag and no restriction; the request's own wording decides between yes and no."""
    if (len(options) != 2 or answer.choice != "none" or answer.probabilities.get("none", 1) > 0.7
            or not flag_values(g.schema.values.get(key, ()))):
        return answer
    word = re.escape(g.schema.column(key).label.split()[0])
    noun = re.escape(lemma(entity(g.schema, g.schema.column(key).table).split()[-1]))
    stated = re.search(rf"\b(?:is|are|was|were|be|been)\s+(not\s+)?{word}\b|\b(non-?|not\s+)?{word}\s+{noun}", g.question, re.I)
    if not stated:
        return answer
    choice = options[1].key if (stated.group(1) or stated.group(2)) else options[0].key
    return Answer("choice", choice, {choice: 0.75, "none": answer.probabilities.get("none", 0.25)}, answer.confidence)


BUT_NOT = re.compile(r"^\W*(?:but|and|,)?\s*(?:not|never|no)\b|^\W*(?:except|excluding|without|rather than)\b",
                     re.I)


def _but_not(g: Grounding, conditions: list[Condition], excluded: list[Condition],
             literal_conditions: list[tuple[Literal, Condition]]
             ) -> tuple[list[Condition], list[Condition]]:
    """'students allergic to milk but not cat': the second value of the same attribute
    is excluded (results having it are removed), not a second filter."""
    for (a, ca), (b, cb) in zip(literal_conditions, literal_conditions[1:]):
        if (ca in conditions and cb in conditions and ca.column and cb.column and ca.kind == cb.kind
                and g.schema.canonical(ca.column) == g.schema.canonical(cb.column)
                and BUT_NOT.search(g.question[a.end:b.start].replace("the ", ""))):
            conditions = [c for c in conditions if c is not cb]
            excluded = excluded + [_positive(cb)]
    return conditions, excluded


def _join_name_parts(g: Grounding, conditions: list[Condition],
                     literal_conditions: list[tuple[Literal, Condition]]) -> list[Condition]:
    """'Michael Goodrich' grounded as first name 'Michael' and last name 'Goodrich', or
    'the Fall of 2009' as semester and year: adjacent values on two columns of one table
    describe one record."""
    out = list(conditions)
    for (a, ca), (b, cb) in zip(literal_conditions, literal_conditions[1:]):
        if (ca in out and cb in out and re.fullmatch(r"\s*(?:of|in|,|-)?\s*", g.question[a.end:b.start], re.I)
                and ca.kind == cb.kind == "compare" and ca.op == cb.op == "="
                and ca.column and cb.column and ca.column != cb.column
                and ca.column.split(".")[0] == cb.column.split(".")[0]):
            position = out.index(ca)
            out = [c for c in out if c not in (ca, cb)]
            out.insert(position, Condition("all", inner=(ca, cb)))
    return out


def _drop_redundant_absent(schema: Schema, conditions: list[Condition],
                           subject: str | None) -> list[Condition]:
    """'employees with no manager': a missing reference column already says there is no
    related record; the anti-join through that same column adds nothing."""
    missing = {c.column for c in conditions if c.kind == "null" and not c.negated and c.column}
    if not missing or subject is None:
        return conditions
    out = []
    for c in conditions:
        if c.kind == "absent" and not c.inner and c.table:
            paths = schema.shortest_paths(subject, c.table, limit=1)
            step = paths[0][0] if paths and paths[0] else None
            if step and step.fk.table == subject and any(
                    f"{subject}.{col}" in missing for col in step.fk.columns):
                continue
        out.append(c)
    return out


def _positive(cond: Condition) -> Condition:
    flip = {"!=": "=", "not_contains": "contains"}
    if cond.op in flip:
        kind = "text" if flip[cond.op] == "contains" else cond.kind
        return Condition(kind, cond.column, flip[cond.op], cond.values, cond.negated, cond.func,
                         cond.table, cond.inner)
    return cond


def _dedupe_absent(schema: Schema, conditions: list[Condition], answers: dict[str, Answer],
                   cands: Candidates, excluded: list[Condition] | None = None) -> list[Condition]:
    """'schools without any goalies': one negated relationship, not one per hop; and no
    bare 'has no orders' next to 'never ordered Espresso Beans' (a narrower exclusion)."""
    absent = [c for c in conditions if c.kind == "absent"]
    specific = [c for c in absent if c.inner]
    if (specific or excluded) and len(absent) > len(specific):
        conditions = [c for c in conditions if not (c.kind == "absent" and not c.inner)]
        absent = specific
    if len(absent) < 2:
        return conditions
    keep = max(absent, key=lambda c: (bool(c.inner), -len(_far_side(schema, next(
        (s for _, s, t in cands.relations if t == c.table), c.table or ""), c.table or ""))))
    merged_inner = tuple(dict.fromkeys(i for c in absent for i in c.inner))
    keep = Condition("absent", None, table=keep.table, inner=merged_inner)
    return [c for c in conditions if c.kind != "absent"] + [keep]


def _groups_are_singletons(schema: Schema, table: str, keys: list[str]) -> bool:
    """True when every group holds one row of ``table`` (grouped by a unique column)."""
    for key in keys:
        for member in schema.equivalents(key):
            if member.split(".")[0] != table:
                continue
            if schema.unique_like(member):
                return True
    return False


def _retarget_self_counts(g: Grounding, cands: Candidates, answers: dict[str, Answer],
                          subject: str | None, order: tuple[Ordering, ...],
                          conditions: list[Condition], group_keys: list[str],
                          outputs: list[OutputItem] | None = None
                          ) -> tuple[tuple[Ordering, ...], list[Condition], list[OutputItem]]:
    """Counting a result's own records per result is always 1 ('the order with the most
    items' counts items, not orders): count the most plausible related table instead."""
    if subject is None:
        return order, conditions, list(outputs or [])
    schema = g.schema

    def related_choice() -> str | None:
        scores: dict[str, float] = {}
        for key, rel_subject, target in cands.relations:
            if rel_subject != subject:
                continue
            answer = answers.get(f"rel_{key}")
            has = answer.probabilities.get("has", 0.0) if answer else 0.0
            scores[target] = max(scores.get(target, 0.0), has + g.table_link.get(target, 0.0))
        return max(scores, key=lambda t: scores[t]) if scores else None

    target = None
    counts_subject = any(o.table == subject and o.func == "COUNT" and o.column is None
                         for o in order) or any(c.kind == "having" and c.table == subject
                                                and not c.column for c in conditions) or any(
        o.table == subject and o.func == "COUNT" and o.column is None for o in outputs or [])
    if counts_subject and _groups_are_singletons(schema, subject, group_keys):
        target = related_choice()
    outputs = list(outputs or [])
    if target is None:
        return order, conditions, outputs
    outputs = [OutputItem("COUNT", None, target) if o.table == subject and o.func == "COUNT"
               and o.column is None else o for o in outputs]
    order = tuple(Ordering(o.func, o.column, target, o.desc)
                  if o.table == subject and o.func == "COUNT" and o.column is None else o
                  for o in order)
    conditions = [Condition(c.kind, c.column, c.op, c.values, c.negated, c.func, target, c.inner)
                  if c.kind == "having" and c.table == subject and not c.column else c
                  for c in conditions]
    return order, conditions, outputs


def _percentage(g: Grounding, outputs: list[OutputItem], conditions: list[Condition],
                subject: str | None, answers: dict[str, Answer], followup: dict[str, Question],
                record: _Recorder) -> tuple[list[OutputItem], list[Condition]]:
    """'What percentage of orders were cancelled?': one condition defines the counted part,
    the others (if any) the whole.  Which one is a semantic choice: ask it."""
    if not PCT_CUE.search(g.question):
        return outputs, conditions
    candidates = [c for c in conditions if c.kind in ("compare", "text", "in_values", "null", "between",
                                                     "period")]
    counts = [o for o in outputs if o.func == "COUNT" and o.column is None and not o.per]
    if not candidates or len(counts) > 1:
        return outputs, conditions
    from intentsql.render import condition as render_condition
    key = "share"
    options = {f"c{i}": "the records " + render_condition(g.schema, c, subject).removeprefix("whose ")
               .removeprefix("that ") for i, c in enumerate(candidates)}
    if key not in answers:
        followup[key] = choice(
            "`request` asks for a percentage (share) of records. Which records make up that share?",
            {**options, "none": "no percentage or share is asked for"})
        return outputs, conditions
    answer = answers[key]
    record(key, "percentage", "share of records", answer, answer.choice, {**options, "none": "none"})
    if answer.choice == "none":
        return outputs, conditions
    chosen = candidates[int(answer.picked[1:])]
    table = counts[0].table if counts else chosen.column.split(".")[0] if chosen.column else subject
    share = OutputItem("PCT", None, table, where=(chosen,))
    rest = [o for o in outputs if o not in counts]
    return ([share] + [o for o in rest if not o.is_aggregate or o.per] if not counts else
            [share if o in counts else o for o in outputs]), [c for c in conditions if c is not chosen]


def _resolve_count_sum(schema: Schema, outputs: list[OutputItem], group_by: list[str],
                       answers: dict[str, Answer], followup: dict[str, Question],
                       record: _Recorder) -> list[OutputItem]:
    """A count and a total of the same records ('the total number of students') are
    usually one request read twice: ask which one is meant."""
    count = next((o for o in outputs if o.func == "COUNT" and o.column is None), None)
    total = next((o for o in outputs if o.func == "SUM" and o.column), None)
    if count is None or total is None:
        return outputs
    key = "conflict_count_sum"
    if key not in answers:
        phrase = column_phrase(schema, total.column or "")
        followup[key] = choice(
            "Which does `request` ask for?",
            {"count": f"how many {entity(schema, count.table or '')} records there are",
             "sum": f"the total {phrase} (adding up the stored values)",
             "both": "both of these, as two separate values"})
        return outputs
    answer = answers[key]
    record(key, "output conflict", "count or total", answer, answer.choice)
    if answer.choice == "count":
        return [o for o in outputs if o is not total]
    if answer.choice == "sum":
        return [o for o in outputs if o is not count]
    return outputs


def _choose_routes(g: Grounding, intent: Intent, answers: dict[str, Answer],
                   followup: dict[str, Question], record: _Recorder) -> Intent:
    """When two needed tables are connected by different routes of equal length
    (instructor-student via advisor or via department), the route is a meaning: use
    the one the request names, otherwise ask."""
    from intentsql.intent import query_tables
    schema = g.schema
    tables = query_tables(intent)
    related = {c.table for c in intent.conditions if c.kind == "related" and c.table}
    via: list[str] = list(intent.via)
    subject = intent.subject
    for target in related:
        # 'students taking ACCT-211': when the request names a relationship (taking ->
        # enrollments), reach the other tables through it rather than a shorter detour.
        for table in tables:
            if not subject or table in (subject, target) or target in tables:
                continue
            direct = schema.shortest_paths(subject, table, limit=1)
            through = schema.shortest_paths(target, table, limit=1)
            first = schema.shortest_paths(subject, target, limit=1)
            if (direct and through and first and target not in {s.table for s in direct[0]}
                    and len(first[0]) + len(through[0]) <= len(direct[0]) + 2):
                via.extend([s.table for s in first[0]] + [s.table for s in through[0]])
    for i, a in enumerate(tables):
        for b in tables[i + 1:]:
            paths = schema.shortest_paths(a, b, limit=4)
            if len(paths) < 2 or len(paths[0]) < 2:
                continue
            routes = {tuple(step.table for step in path[:-1]) for path in paths}
            if len(routes) < 2:
                continue
            routes_list = sorted(routes)
            named = [r for r in routes_list if any(t in related or g.table_link.get(t, 0) > 0
                                                    for t in r)]
            if len(named) == 1:
                via.extend(named[0])
                continue
            key = f"route_{a}|{b}"
            options = {f"p{k}": (f"{entity(schema, a)} and {entity(schema, b)} related through "
                                 + " and ".join(entity(schema, t) for t in route))
                       for k, route in enumerate(routes_list)}
            if key not in answers:
                followup[key] = choice(
                    f"How does `request` relate {entity(schema, a)} and {entity(schema, b)}?",
                    options)
                continue
            answer = answers[key]
            route = routes_list[int(answer.picked[1:])]
            record(key, "join route", options[answer.picked], answer, route)
            via.extend(route)
    if tuple(via) == intent.via:
        return intent
    return intent.with_(via=tuple(dict.fromkeys(via)))


def _categorical(schema: Schema, key: str) -> bool:
    """A column whose values repeat across rows ('gender', 'rank'): a sensible group key.
    Measures and per-record labels are not, even in a table too small to tell."""
    column = schema.column(key)
    rows = schema.tables[column.table].row_count
    if not column.distinct_count or column.distinct_count >= max(2, 0.9 * rows):
        return False
    return column.kind != NUMBER or column.distinct_count <= 12


def _ungrouped(schema: Schema, item: OutputItem, group_by: list[str]) -> bool:
    """A plain value that varies within a group (allergy name per allergy type) has no
    single value to show; values of a record that the group identifies are fine."""
    if item.is_aggregate or not item.column or item.star:
        return False
    if any(item.column in schema.equivalents(k) for k in group_by):
        return False
    table = item.column.split(".")[0]
    for key in group_by:
        for member in schema.equivalents(key):
            if member.split(".")[0] == table and _identifies_rows(
                    schema, table, [OutputItem(None, member)]):
                return False
    return True


def _group_label(schema: Schema, key: str, outputs: list[OutputItem],
                 condition_tables: set[str] | None = None) -> str | None:
    """How to show a group keyed by an identifier: the name of the record it identifies.

    'products per supplier' groups by products.supplier_id, which identifies a supplier:
    show the supplier's name (joining suppliers), never a product column."""
    column = schema.column(key)
    if not column.is_identifier:
        return key
    canon = schema.canonical(key)
    owner = schema.table(canon.split(".")[0] if schema.column(canon).primary_key else column.table)
    if any(o.column and o.column.split(".")[0] == owner.name and not o.is_aggregate for o in outputs):
        return None   # the entity is already shown through one of its attributes
    for col in owner.columns:
        if col.kind == TEXT and re.search(r"\b(name|title)\b", col.label):
            return col.key
    return key


def _prefer_named_tables(g: Grounding, outputs: list[OutputItem]) -> list[OutputItem]:
    """'the names of all the teachers': when the same attribute exists in several
    tables and the request names one of them, read it from the named table."""
    schema = g.schema
    chosen_tables = {t for o in outputs for t in o.tables}
    result = []
    for item in outputs:
        if item.column and not g.table_named.get(item.column.split(".")[0], 0):
            label = schema.column(item.column).label
            named = [c.key for t, tl in g.table_named.items() if tl > 0 and t not in chosen_tables
                     for c in schema.table(t).columns if c.label == label]
            if len(named) == 1 and not any(t in chosen_tables and g.table_named.get(t, 0) > 0
                                           for t in chosen_tables):
                item = replace(item, column=named[0])
        result.append(item)
    return result


def _numeric_op(op: str | None) -> str:
    return op if op in NUMERIC_OPS else "="


def _referenced_tables(outputs: list[OutputItem], conditions: list[Condition]) -> set[str]:
    tables = {t for o in outputs for t in o.tables}
    tables.update(t for c in conditions for t in c.tables)
    return tables


def _shape_outputs(g: Grounding, outputs: list[OutputItem], order: tuple[Ordering, ...],
                   limit: int | None, group_by: list[str], conditions: list[Condition]
                   ) -> list[OutputItem]:
    schema = g.schema
    # Superlative rows ('the model with the least RAM, and its RAM'): plain values under
    # ORDER BY ... LIMIT, not MIN()/MAX() aggregates with an accidental GROUP BY.
    if order and limit is not None and not group_by and not order[0].is_aggregate:
        if any(not o.is_aggregate for o in outputs):
            outputs = [OutputItem(None, o.column) if o.func in ("MIN", "MAX") and o.column else o
                       for o in outputs]
    deduped: list[OutputItem] = []
    for item in outputs:
        if item not in deduped:
            deduped.append(item)
    # Same-label columns of different tables ('title' of film and of film_text) are
    # near-synonyms: keep one unless the request names both tables.
    by_label: dict[tuple[str | None, str], list[OutputItem]] = {}
    for item in deduped:
        if item.column:
            by_label.setdefault((item.func, schema.column(item.column).label), []).append(item)
    for items in by_label.values():
        if len(items) < 2:
            continue
        others = [o for o in deduped if o not in items]
        needed = _referenced_tables(others, conditions)
        named = [i for i in items if g.table_link.get((i.column or "").split(".")[0], 0) >= 1.0]
        if len(named) >= 2:
            continue
        keep = [i for i in items if (i.column or "").split(".")[0] in needed][:1] or named[:1] or items[:1]
        deduped = [o for o in deduped if o not in items or o in keep]
    return deduped


def _far_side(schema: Schema, subject: str, target: str) -> set[str]:
    paths = schema.shortest_paths(subject, target, limit=1)
    return {step.table for step in paths[0]} if paths else {target}


def _comparable(value: Any) -> bool:
    return isinstance(value, (int, float)) or (isinstance(value, str) and bool(
        re.match(r"^\d{4}-\d{1,2}-\d{1,2}", value)))


def _text_between(g: Grounding, a: Any, b: Any) -> str | None:
    spans = {}
    for lit in g.literals:
        for value in (a, b):
            if lit.value == value and value not in spans:
                spans[value] = (lit.start, lit.end)
    if len(spans) < 2:
        return None
    (s1, e1), (s2, e2) = sorted(spans.values())
    return g.question[e1:s2]


def _identifies_rows(schema: Schema, table: str, outputs: list[OutputItem]) -> bool:
    t = schema.table(table)
    for o in outputs:
        if o.column and not o.is_aggregate and o.column.split(".")[0] == t.name:
            if schema.unique_like(o.column):
                return True
    return False


def _normalize_conditions(g: Grounding, conditions: list[Condition], connector: str,
                          subject: str | None, outputs: list[OutputItem] | None = None
                          ) -> tuple[list[Condition], str, tuple[int, int] | None]:
    """Deterministic clean-ups that need no semantic judgment."""
    if RANGE_CUE.search(g.question):
        lows = [c for c in conditions if c.kind == "compare" and c.op in (">=", ">", "=")]
        highs = [c for c in conditions if c.kind == "compare" and c.op in ("<=", "<", "=")]
        for low in lows:
            for high in highs:
                if (low is not high and low.column == high.column and low in conditions
                        and high in conditions and _comparable(low.values[0])
                        and _comparable(high.values[0])
                        and type(low.values[0]) is type(high.values[0])):
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
            by_column.setdefault(g.schema.canonical(c.col), []).append(c)
    for conds in by_column.values():
        if len(conds) != 2 or conds[0].values == conds[1].values:
            continue
        between = _text_between(g, conds[0].values[0], conds[1].values[0])
        wants_both = (connector == "AND" if between is None else
                      bool(re.search(r"\b(and|both|as well as)\b", between, re.I))
                      and not re.search(r"\bor\b", between, re.I))
        table = conds[0].col.split(".")[0]
        if wants_both and BOTH_CUE.search(g.question):
            # 'customers with both saving and checking accounts': no single row holds two
            # values, so the two values come from different rows of the same result.
            both = (conditions.index(conds[0]), conditions.index(conds[1]))
        elif wants_both and table != subject:
            both = (conditions.index(conds[0]), conditions.index(conds[1]))
        elif wants_both and subject and not _identifies_rows(g.schema, subject, outputs or []):
            # 'studios with films by both A and B': two different rows of the same table.
            both = (conditions.index(conds[0]), conditions.index(conds[1]))
        else:
            # One row cannot hold two values: 'singers from France and Germany' means either.
            others = [c for c in conditions if c not in conds]
            merged = Condition("in_values", conds[0].column, values=tuple(c.values[0] for c in conds))
            conditions = others + [merged]
    compounds: dict[tuple[str, ...], list[Condition]] = {}
    for c in conditions:
        if c.kind == "all":
            compounds.setdefault(tuple(sorted(g.schema.canonical(i.column or "") for i in c.inner)), []).append(c)
    for pair in compounds.values():
        if len(pair) != 2 or both is not None:
            continue
        first, second = pair
        between = _text_between(g, first.inner[-1].values[0], second.inner[0].values[0])
        if between is not None and re.search(r"\bor\b", between, re.I):
            # 'reported by Dameon Frami or Jolie Weber': either person.
            conditions = [c for c in conditions if c not in pair] + [Condition("any", inner=(first, second))]
        elif between is not None and (BOTH_CUE.search(g.question) or connector == "AND"):
            both = (conditions.index(first), conditions.index(second))
    if both is None and connector == "AND":
        both = _disjoint_ranges(g.schema, conditions)
    return conditions, connector, both


def _disjoint_ranges(schema: Schema, conditions: list[Condition]) -> tuple[int, int] | None:
    """'types with products priced above 4500 and products priced below 3000': two ranges
    of one column that no single row can satisfy together describe different rows."""
    ranges = [(i, c) for i, c in enumerate(conditions) if c.kind == "compare"
              and c.op in (">", ">=", "<", "<=") and isinstance(c.values[0], (int, float))]
    for i, low in ranges:
        for j, high in ranges:
            if (low.op in (">", ">=") and high.op in ("<", "<=")
                    and schema.canonical(low.col) == schema.canonical(high.col)
                    and low.values[0] >= high.values[0]):
                return (min(i, j), max(i, j))
    return None


def _ordinal_offset(question: str) -> int | None:
    match = re.search(r"\b(second|third|fourth|fifth)\s+(\w+est|most|highest|lowest|largest|"
                      r"smallest|youngest|oldest)\b", question, re.I)
    return ORDINALS[match.group(1).lower()] - 1 if match else None


# ---- FK-equivalent columns ------------------------------------------------------------

def rebind_equivalents(schema: Schema, intent: Intent) -> Intent:
    """Choose, jointly for all FK-equivalent columns, the members that make the join
    tree smallest (a card's customer id is read from the card, not via a join)."""
    from itertools import product
    keys: list[str] = [o.column for o in intent.outputs if o.column]
    keys += [c.column for c in intent.conditions if c.column and c.kind not in ("absent", "same_as")]
    keys += list(intent.group_by) + [o.column for o in intent.order if o.column]
    keys = list(dict.fromkeys(keys))
    fixed: list[str] = []
    if any(c.kind == "absent" for c in intent.conditions) and intent.subject:
        fixed.append(intent.subject)
    fixed += [o.table for o in intent.outputs if o.column is None and o.table]
    fixed += [c.table for c in intent.conditions
              if c.kind in ("related", "having") and c.table and not c.column]
    fixed += [c.column.split(".")[0] for c in intent.conditions if c.kind == "same_as" and c.column]
    fixed += list(intent.via)
    flexible = [k for k in keys if len(schema.equivalents(k)) > 1]
    fixed += [k.split(".")[0] for k in keys if k not in flexible]
    fixed = list(dict.fromkeys(fixed))
    if not flexible:
        return intent
    options = [schema.equivalents(k)[:6] for k in flexible]
    best: tuple[tuple[int, int], tuple[str, ...]] | None = None
    combos = 0
    for combo in product(*options):
        combos += 1
        if combos > 512:
            break
        tables = list(dict.fromkeys(fixed + [m.split(".")[0] for m in combo]))
        tree = schema.join_tree(tables, tables[0])
        cost = (len(tree) if tree is not None else 99,
                sum(1 for k, m in zip(flexible, combo) if k != m))
        if best is None or cost < best[0]:
            best = (cost, combo)
    assert best is not None
    mapping = dict(zip(flexible, best[1]))
    if all(mapping[k] == k for k in flexible):
        return intent

    def m(key: str | None) -> str | None:
        return mapping.get(key, key) if key else key

    outputs_list: list[OutputItem] = []
    for o in intent.outputs:
        item = replace(o, column=m(o.column), where=tuple(replace(c, column=m(c.column)) for c in o.where))
        if item in outputs_list or (item.column and not item.func and any(
                x.column and not x.func and x.column in schema.equivalents(item.column)
                for x in outputs_list)):
            continue   # the same value read twice through a foreign key
        outputs_list.append(item)
    outputs = tuple(outputs_list)
    conditions = tuple(Condition(c.kind, m(c.column), c.op, c.values, c.negated, c.func, c.table,
                                 c.inner) for c in intent.conditions)
    order = tuple(Ordering(o.func, m(o.column), o.table, o.desc) for o in intent.order)
    subject = intent.subject
    if outputs and outputs[0].tables and not any(c.kind == "absent" for c in conditions):
        subject = outputs[0].tables[0]
    return intent.with_(outputs=outputs, conditions=conditions,
                        group_by=tuple(m(k) for k in intent.group_by), order=order, subject=subject)


# ---- entry point -------------------------------------------------------------------------

def _ask_debiased(ask: AskFn, state: dict[str, Any], questions: dict[str, Question]
                  ) -> dict[str, Answer]:
    """Ask every multi-option Choice also with its options reversed; average the two
    distributions to cancel Jev's documented lean toward the first option."""
    twins = {}
    for key, q in questions.items():
        if q.kind == "choice" and q.options and len(q.options) > 2:
            twins[f"{key}~r"] = Question("choice", q.instructions,
                                         dict(reversed(list(q.options.items()))))
    answers = ask(state, {**questions, **twins}, "interpretation")
    merged: dict[str, Answer] = {}
    for key, answer in answers.items():
        if key.endswith("~r"):
            continue
        twin = answers.get(f"{key}~r")
        if twin is None:
            merged[key] = answer
            continue
        probs = {k: (answer.probabilities.get(k, 0.0) + twin.probabilities.get(k, 0.0)) / 2
                 for k in set(answer.probabilities) | set(twin.probabilities)}
        best = max(probs, key=lambda k: probs[k])
        merged[key] = Answer("choice", best, probs, None)
    return merged



AskFn = Callable[[dict[str, Any], dict[str, Question], str], dict[str, Answer]]


def interpret(g: Grounding, ask: AskFn, strategy: Strategy | None = None
              ) -> tuple[Interpretation, Candidates]:
    strategy = strategy or Strategy()
    cands = build_candidates(g, strategy)
    state = _state(g, cands, strategy)
    questions = round_one(g, cands, strategy)
    questions_asked = dict(questions)
    if strategy.debias_order:
        answers = _ask_debiased(ask, state, questions)
    else:
        answers = ask(state, questions, "interpretation")
    interpretation = assemble(g, cands, answers, strategy)
    rounds = 1
    while interpretation.followup and rounds < MAX_ROUNDS + 1:
        questions_asked.update(interpretation.followup)
        answers.update(ask(state, interpretation.followup, "follow-up"))
        interpretation = assemble(g, cands, answers, strategy)
        rounds += 1
    if interpretation.intent is None and not interpretation.message:
        interpretation.message = "Could not determine what the request asks to return."
    interpretation.answers = answers
    _attach_options(interpretation.decisions, questions_asked)
    return interpretation, cands


def _attach_options(decisions: list[Decision], questions: dict[str, Question]) -> None:
    """Give every recorded Choice its option texts, for the debug view."""
    for decision in decisions:
        question = questions.get(decision.key)
        if decision.options is None and question is not None and question.kind == "choice":
            decision.options = {k: v if isinstance(v, str) else str(v)
                                for k, v in (question.options or {}).items()}


def alternatives(g: Grounding, cands: Candidates, interpretation: Interpretation,
                 strategy: Strategy | None = None, limit: int = 4,
                 margin: float = 0.5) -> list[tuple[str, Intent]]:
    """Intents obtained by flipping one low-margin decision to its runner-up option.

    The same deterministic assembly runs on the modified answers, so every
    alternative is as well-formed as the original; nothing new is invented.
    """
    strategy = strategy or Strategy()
    answers = interpretation.answers
    ranked = []
    for key, answer in answers.items():
        if answer.kind == "choice" and len(answer.probabilities) >= 2:
            (top, p1), (second, p2) = answer.ranked()[:2]
            if p1 - p2 < margin and p2 > 0.02:
                ranked.append((p1 - p2, key, Answer("choice", second, answer.probabilities,
                                                    answer.confidence)))
        elif answer.kind == "noul" and answer.p is not None and abs(answer.p - 0.5) < margin / 2:
            ranked.append((abs(answer.p - 0.5) * 2, key, Answer("noul", p=1 - answer.p)))
    ranked.sort(key=lambda r: r[0])
    out: list[tuple[str, Intent]] = []
    seen = {interpretation.intent}
    for _, key, flipped in ranked:
        if len(out) >= limit:
            break
        alt = assemble(g, cands, {**answers, key: flipped}, strategy)
        if alt.intent is not None and alt.intent not in seen and not alt.followup:
            seen.add(alt.intent)
            out.append((key, alt.intent))
    return out
