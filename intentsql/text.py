"""Small, dependency-free text utilities shared by grounding and interpretation."""

from __future__ import annotations

import re
from dataclasses import dataclass

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# "employee's" is the word "employee" (a possessive is not part of the word); "don't" stays whole.
_WORD = re.compile(r"[A-Za-z]+(?:'(?!s\b)[A-Za-z]+)?|\d+(?:[.,]\d+)*")
_POSSESSIVE_S = re.compile(r"(?<=[A-Za-z])['’]s\b")

ABBREVIATIONS = {
    "id": "id", "num": "number", "no": "number", "nbr": "number", "qty": "quantity",
    "amt": "amount", "dept": "department", "dob": "date of birth", "addr": "address",
    "desc": "description", "pct": "percent", "avg": "average", "yr": "year",
    "stu": "student", "emp": "employee", "fname": "first name", "lname": "last name",
    "mgr": "manager", "cnt": "count", "pos": "position", "max": "maximum", "min": "minimum",
}


def humanize(identifier: str) -> str:
    """``Song_release_year`` -> ``song release year``; ``StuID`` -> ``stu id``."""
    spaced = _CAMEL.sub(" ", identifier.replace("_", " ").replace("-", " "))
    words = [w.lower() for w in spaced.split() if w]
    return " ".join(words) if words else identifier


_IRREGULAR = {
    "people": "person", "children": "child", "men": "man", "women": "woman",
    "feet": "foot", "teeth": "tooth", "mice": "mouse", "geese": "goose", "data": "data",
    "media": "medium", "criteria": "criterion", "indices": "index", "series": "series",
    "species": "species", "staff": "staff", "news": "news", "analyses": "analysis",
}
_KEEP_S = ("ss", "us", "is", "ous", "ics")


def lemma(word: str) -> str:
    """A conservative singularizer/normalizer for matching (not linguistics)."""
    w = word.lower()
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("ches", "shes", "sses", "xes", "zes", "uses")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(_KEEP_S):
        return w[:-1]
    return w


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int

    @property
    def lower(self) -> str:
        return self.text.lower()

    @property
    def lemma(self) -> str:
        return lemma(self.text)


def tokenize(text: str) -> list[Token]:
    possessive = {m.start() + 1 for m in _POSSESSIVE_S.finditer(text)}
    return [Token(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text)
            if m.start() not in possessive]


def words(text: str) -> list[str]:
    return [lemma(t.text) for t in tokenize(humanize(text) if "_" in text else text)]


STOPWORDS = frozenset("""
a an the of in on at to for by with from and or is are was were be been being what which who
whom whose that this these those there their its it as all any each every me show list give
find return display tell how many much do does did have has had please also than then
get name names i we you they our your his her them out into about only per
""".split())


NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
    "dozen": 12, "once": 1, "twice": 2, "thrice": 3,
}
ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10,
}


def stem(word: str) -> str:
    """Lemma plus crude verb-suffix stripping, for lexical linking only."""
    w = lemma(word)
    for suffix in ("ing", "ed"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            w = w[: -len(suffix)]
            if len(w) >= 4 and w[-1] == w[-2] and w[-1] not in "aeiouls":
                w = w[:-1]
            break
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w
