"""Natural language -> Intent, using Jev only for bounded, grounded decisions.

Round 1 is one speculative fan-out: every decision the request might need is
asked at once, over candidates that code generates from the schema and the
request's grounded literals.  Alternatives that exclude each other compete in a
single Choice (never as independent yes/no questions, which over-select
near-synonyms).  Dependent decisions are asked speculatively for every parent
option, and only the branch selected by the parent is consumed.

Candidates come from candidates.py, the questions from questions.py and the cue
words from cues.py.  This module runs the rounds and assembles the answers into an ``Intent``.  Join paths, grouping keys,
subquery shapes and FK-equivalent column choices are derived deterministically.
When assembly finds a gap (e.g. nothing to return), it issues a targeted
follow-up round instead of guessing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from intentsql.candidates import Candidates, Decision, Option, Strategy, build_candidates
from intentsql.cues import (
    BOTH_CUE,
    DISTINCT_CUE,
    EACH_CUE,
    EXCEPT_CUE,
    KEEP_EMPTY_CUE,
    MAX_ROUNDS,
    NUMERIC_OPS,
    PCT_CUE,
    RANGE_CUE,
)
from intentsql.grounding import Grounding, Literal
from intentsql.intent import Condition, Intent, Ordering, OutputItem, _negate, column_phrase, entity
from intentsql.jev import Answer, Question, choice
from intentsql.questions import _state, missing_output_question, round_one
from intentsql.schema import DATE, NUMBER, TEXT, Schema, flag_values
from intentsql.text import ORDINALS, lemma

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
        main_options = missing_output_question(cands)["main_output"].options or {}
        record("main_output", "output", str(main_options.get(key, key)), answers["main_output"], "value")
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
    conditions = _drop_reference_dates(schema, outputs, order, conditions)
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


def _drop_reference_dates(schema: Schema, outputs: list[OutputItem], order: tuple[Ordering, ...],
                          conditions: list[Condition]) -> list[Condition]:
    """'years worked by the end of 2024': the year is the reference date of the elapsed
    time being computed, not a filter on a date column."""
    years = set()
    for key in [o.column for o in outputs if o.column] + [o.column for o in order if o.column]:
        formula = schema.column(key).formula
        if formula and formula[0].startswith("since_") and formula[2].startswith("lit:"):
            years.add(int(formula[2][4:8]))
    if not years:
        return conditions
    return [c for c in conditions if not (
        c.kind == "compare" and c.column and c.values and c.values[0] in years
        and (schema.column(c.column).kind == DATE or "year" in schema.column(c.column).label
             or schema.column(c.column).formula is not None))]


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
