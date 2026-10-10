"""Cue words and operator wordings used to decide which questions to ask Jev.

Cues only gate which candidate decisions are offered; Jev still makes every decision.
"""

from __future__ import annotations

import re

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
