"""Ground a natural-language request in the database: literals, values, links.

Everything here is deterministic.  The output says *what the request could be
referring to* (stored values, numbers, schema words); deciding what it *does*
refer to is left to the interpreter.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from intentsql.schema import NUMBER, TEXT, Column, Schema, quote_identifier
from intentsql.text import NUMBER_WORDS, STOPWORDS, Token, lemma, stem, tokenize

_QUOTED = re.compile(r"\"([^\"]+)\"|“([^”]+)”|‘([^’]+)’|(?<![A-Za-z])'([^']+)'(?![A-Za-z])")
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w])")
_DATE = re.compile(r"(?<![\w-])\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?(?![\w-])")
MAX_NGRAM = 8


@dataclass(frozen=True)
class Literal:
    id: str
    text: str                      # surface text in the request
    value: Any                     # number, or canonical stored text
    kind: str                      # "number" | "text" | "date"
    start: int
    end: int
    columns: tuple[str, ...] = ()  # column keys known to store this value
    exact: bool = False            # value found verbatim (case-insensitive) in the database
    quoted: bool = False
    alternatives: tuple[tuple[str, str], ...] = ()   # (column key, stored value) variants

    def describe(self) -> str:
        return str(self.text) if self.kind == "number" else f'"{self.text}"'


@dataclass
class Grounding:
    question: str
    schema: Schema
    tokens: list[Token]
    literals: list[Literal]
    column_link: dict[str, float] = field(default_factory=dict)
    table_link: dict[str, float] = field(default_factory=dict)
    mention_position: dict[str, int] = field(default_factory=dict)

    def literal(self, literal_id: str) -> Literal:
        for literal in self.literals:
            if literal.id == literal_id:
                return literal
        raise KeyError(literal_id)

    @property
    def lemmas(self) -> list[str]:
        return [t.lemma for t in self.tokens]


# ---- value index --------------------------------------------------------------

@dataclass(frozen=True)
class ValueIndex:
    exact: dict[str, tuple[tuple[str, str], ...]]   # normalized -> ((column key, stored value), ...)
    by_first_word: dict[str, tuple[str, ...]]       # first word -> normalized values


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


_INDEXES: dict[int, ValueIndex] = {}


def value_index(schema: Schema) -> ValueIndex:
    cached = _INDEXES.get(id(schema))
    if cached is not None:
        return cached
    exact: dict[str, list[tuple[str, str]]] = {}
    for key, values in schema.values.items():
        for value in values:
            norm = _normalize(value)
            if not norm or len(norm) > 120:
                continue
            exact.setdefault(norm, []).append((key, value))
    by_first: dict[str, list[str]] = {}
    for norm in exact:
        by_first.setdefault(norm.split(" ", 1)[0], []).append(norm)
    index = ValueIndex({k: tuple(v) for k, v in exact.items()},
                       {k: tuple(v) for k, v in by_first.items()})
    if len(_INDEXES) > 64:
        _INDEXES.clear()
    _INDEXES[id(schema)] = index
    return index


# ---- extraction -----------------------------------------------------------------

def _parse_number(text: str) -> int | float:
    cleaned = text.replace(",", "")
    number = float(cleaned)
    return int(number) if number.is_integer() and "." not in cleaned else number


def _schema_words(schema: Schema) -> set[str]:
    words = set()
    for table in schema.tables.values():
        words.update(lemma(w) for w in table.label.split())
        for column in table.columns:
            words.update(lemma(w) for w in column.label.split())
    return words


def _probe_large_columns(schema: Schema, text: str) -> list[tuple[str, str]]:
    """Exact (case-insensitive) lookup in text columns too large for the value index."""
    found: list[tuple[str, str]] = []
    if not schema.path or not Path(schema.path).is_file():
        return found
    conn = sqlite3.connect(Path(schema.path).as_uri() + "?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    try:
        for column in schema.columns:
            if column.kind != TEXT or column.key in schema.values:
                continue
            table, name = quote_identifier(column.table), quote_identifier(column.name)
            row = conn.execute(f"SELECT {name} FROM {table} WHERE {name} = ? COLLATE NOCASE "
                               f"LIMIT 1", (text,)).fetchone()
            if row:
                found.append((column.key, str(row[0])))
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return found


def extract_literals(question: str, schema: Schema) -> list[Literal]:
    index = value_index(schema)
    tokens = tokenize(question)
    schema_words = _schema_words(schema)
    taken: list[tuple[int, int]] = []
    found: list[Literal] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in taken)

    def add(text: str, value: Any, kind: str, start: int, end: int, matches=(), *,
            quoted: bool = False, exact: bool = False) -> None:
        columns = tuple(dict.fromkeys(key for key, _ in matches))
        found.append(Literal("", text, value, kind, start, end, columns, exact, quoted,
                             tuple(matches)))
        taken.append((start, end))

    # 1. Quoted strings are always literals.
    for match in _QUOTED.finditer(question):
        text = next(g for g in match.groups() if g is not None)
        if not text.strip():
            continue
        matches = index.exact.get(_normalize(text), ()) or tuple(_probe_large_columns(schema, text))
        stored = matches[0][1] if matches else text
        if _NUMBER.fullmatch(text.strip()) and not matches:
            add(text, _parse_number(text.strip()), "number", match.start(), match.end(), quoted=True)
        else:
            add(text, stored, "text", match.start(), match.end(), matches, quoted=True,
                exact=bool(matches))

    # 1b. ISO dates are single text literals, not three numbers.
    for match in _DATE.finditer(question):
        if free(match.start(), match.end()):
            text = match.group(0)
            matches = index.exact.get(_normalize(text), ())
            add(text, matches[0][1] if matches else text, "date", match.start(), match.end(),
                matches, exact=bool(matches))

    # 2. Longest stored-value matches over token n-grams.
    spans = []
    for i in range(len(tokens)):
        for j in range(min(len(tokens), i + MAX_NGRAM), i, -1):
            start, end = tokens[i].start, tokens[j - 1].end
            surface = question[start:end]
            norm = _normalize(surface)
            matches = index.exact.get(norm)
            if not matches:
                continue
            words = [t.lower for t in tokens[i:j]]
            if all(w in STOPWORDS for w in words):
                continue
            if j - i == 1:
                word = tokens[i].text
                # Short or ordinary words only count when written like a value.
                if len(word) <= 2 and not word.isupper() and not word.isdigit():
                    continue
                if (word.islower() and lemma(word) in schema_words and not word.isdigit()):
                    continue
                if word.isdigit():
                    continue  # numbers are handled below, with value matches attached
            spans.append((j - i, start, end, surface, matches))
            break
    for _, start, end, surface, matches in sorted(spans, key=lambda s: (-s[0], s[1])):
        if free(start, end):
            add(surface, matches[0][1], "text", start, end, matches, exact=True)

    # 3. Numbers (digits and number words).
    for match in _NUMBER.finditer(question):
        if not free(match.start(), match.end()):
            continue
        text = match.group(0)
        matches = index.exact.get(_normalize(text), ())
        add(text, _parse_number(text), "number", match.start(), match.end(), matches,
            exact=bool(matches))
    for token in tokens:
        value = NUMBER_WORDS.get(token.lower)
        if value is None or not free(token.start, token.end):
            continue
        if token.lower == "one" and _one_is_pronoun(question, token):
            continue
        add(token.text, value, "number", token.start, token.end)

    # 4. Capitalized spans not found verbatim: fuzzy candidates (typos, case).
    for start, end, surface in _capitalized_spans(question, tokens):
        if not free(start, end) or surface.lower() in STOPWORDS:
            continue
        if lemma(surface.lower()) in schema_words and " " not in surface:
            continue
        matches = _fuzzy(index, surface) or _probe_large_columns(schema, surface)
        if matches:
            add(surface, matches[0][1], "text", start, end, matches)

    found.sort(key=lambda lit: lit.start)
    return [Literal(f"L{i + 1}", *(_fields(lit)[1:])) for i, lit in enumerate(found)]


def _fields(lit: Literal) -> tuple:
    return (lit.id, lit.text, lit.value, lit.kind, lit.start, lit.end, lit.columns, lit.exact,
            lit.quoted, lit.alternatives)


def _one_is_pronoun(question: str, token: Token) -> bool:
    after = question[token.end:token.end + 12].lower()
    before = question[max(0, token.start - 6):token.start].lower()
    return (after.startswith(" of") or after.startswith(" with") or after.startswith(" that")
            or after.startswith(" who") or after.startswith(" which") or "the " in before
            or after.strip() == "" or after.startswith("?") or after.startswith("."))


def _capitalized_spans(question: str, tokens: list[Token]) -> list[tuple[int, int, str]]:
    spans = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.text[:1].isupper() and token.start > 0:
            j = i + 1
            while j < len(tokens) and tokens[j].text[:1].isupper():
                j += 1
            start, end = token.start, tokens[j - 1].end
            spans.append((start, end, question[start:end]))
            i = j
        else:
            i += 1
    return spans


def _fuzzy(index: ValueIndex, surface: str) -> list[tuple[str, str]]:
    norm = _normalize(surface)
    if len(norm) < 4:
        return []
    pool = [v for v in index.exact if abs(len(v) - len(norm)) <= max(2, len(norm) // 4)]
    close = difflib.get_close_matches(norm, pool, n=3, cutoff=0.86)
    out: list[tuple[str, str]] = []
    for value in close:
        out.extend(index.exact[value])
    if not out:
        # Containment: "Anguilla" for stored "Anguilla (UK)" style values.
        contained = [v for v in pool if norm in v.split(" (")[0]][:3]
        for value in contained:
            out.extend(index.exact[value])
    return out


# ---- lexical linking ----------------------------------------------------------------

def link_scores(question: str, schema: Schema) -> tuple[dict[str, float], dict[str, float],
                                                          dict[str, int]]:
    """Share of each label's content words present in the request (0..1)."""
    tokens = tokenize(question)
    lemmas = [stem(t.text) for t in tokens]
    positions: dict[str, int] = {}
    for i, l in enumerate(lemmas):
        positions.setdefault(l, tokens[i].start)
    present = set(lemmas)

    def score(label: str) -> tuple[float, int | None]:
        words = [stem(w) for w in label.split() if w not in STOPWORDS and w != "id"]
        if not words:
            return 0.0, None
        hits = [w for w in words if w in present]
        first = min((positions[w] for w in hits), default=None)
        return len(hits) / len(words), first

    columns: dict[str, float] = {}
    tables: dict[str, float] = {}
    mention: dict[str, int] = {}
    for table in schema.tables.values():
        tables[table.name], pos = score(table.label)
        if pos is not None:
            mention[table.name] = pos
        for column in table.columns:
            columns[column.key], pos = score(column.label)
            if pos is not None:
                mention[column.key] = pos
    return columns, tables, mention


def ground(question: str, schema: Schema) -> Grounding:
    columns, tables, mention = link_scores(question, schema)
    literals = extract_literals(question, schema)
    for literal in literals:
        for key in literal.columns:
            columns[key] = max(columns.get(key, 0.0), 0.9)
            table = key.split(".", 1)[0]
            tables[table] = max(tables.get(table, 0.0), 0.5)
            mention.setdefault(key, literal.start)
    return Grounding(question, schema, tokenize(question), literals, columns, tables, mention)
