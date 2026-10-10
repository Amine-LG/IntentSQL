"""The bounded questions asked of Jev in the main round, and the shared decision state.
"""

from __future__ import annotations

import re
from typing import Any

from intentsql.candidates import Candidates, Strategy
from intentsql.cues import AGG_FUNCS, ALWAYS_CUE, NEGATION_CUE, THEN_CUE
from intentsql.grounding import Grounding
from intentsql.intent import column_phrase, entity
from intentsql.jev import Question, choice, noul
from intentsql.schema import TEXT, Column, flag_values

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
