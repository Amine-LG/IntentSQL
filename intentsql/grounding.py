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
from datetime import date, timedelta
from itertools import product
from pathlib import Path
from typing import Any

from intentsql.measures import derive
from intentsql.schema import TEXT, Schema, quote_identifier
from intentsql.text import NUMBER_WORDS, ORDINALS, STOPWORDS, Token, lemma, stem, tokenize

_QUOTED = re.compile(r"\"([^\"]+)\"|“([^”]+)”|‘([^’]+)’|(?<![A-Za-z])'([^']+)'(?![A-Za-z])")
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w])")
_DATE = re.compile(r"(?<![\w-])\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?(?![\w-])")
_MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
_MONTH_RE = "(" + "|".join(list(_MONTHS) + [m[:3] for m in _MONTHS]) + r")\.?"
_NATURAL_DATE = re.compile(
    rf"\b{_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b|"
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RE},?\s+(\d{{4}})\b", re.I)
_LETTER = re.compile(r"\b(?:letter|character)\s+['\"]?([A-Za-z])['\"]?(?![A-Za-z])", re.I)
MAX_NGRAM = 8


@dataclass(frozen=True)
class Literal:
    id: str
    text: str                      # surface text in the request
    value: Any                     # number, or canonical stored text
    kind: str                      # "number" | "text" | "date" | "period" (value: first day, day after, label)
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
    table_named: dict[str, float] = field(default_factory=dict)   # lexical links only (no literal boost)

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


def _singulars(norm: str) -> list[str]:
    forms = [norm[:-1]]
    if norm.endswith("es"):
        forms.append(norm[:-2])
    if norm.endswith("ies"):
        forms.append(norm[:-3] + "y")
    return forms


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


_INDEXES: dict[int, tuple[dict[str, tuple[str, ...]], ValueIndex]] = {}


def value_index(schema: Schema) -> ValueIndex:
    # Keyed by the identity of the value map (shared by request-scoped schema copies);
    # the map is kept alongside so a recycled id can never return another schema's index.
    cached = _INDEXES.get(id(schema.values))
    if cached is not None and cached[0] is schema.values:
        return cached[1]
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
    _INDEXES[id(schema.values)] = (schema.values, index)
    return index


# ---- extraction -----------------------------------------------------------------

def _parse_number(text: str) -> int | float:
    cleaned = text.replace(",", "")
    number = float(cleaned)
    return int(number) if number.is_integer() and "." not in cleaned else number


def _schema_words(schema: Schema) -> set[str]:
    words: set[str] = set()
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
        parts = None if matches else _name_parts(index, question, match.start(1) if match.group(1)
                                                 else match.start(), match.end(1) if match.group(1)
                                                 else match.end())
        if parts:
            for part_start, part_end, part_matches in parts:
                add(question[part_start:part_end], part_matches[0][1], "text", part_start, part_end,
                    part_matches, quoted=True, exact=True)
            taken.append((match.start(), match.end()))
            continue
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

    # 1c. Natural dates ('June 21, 2002') become ISO literals; 'the letter m' a text literal.
    for match in _NATURAL_DATE.finditer(question):
        if not free(match.start(), match.end()):
            continue
        g = match.groups()
        month, day, year = ((g[0], g[1], g[2]) if g[0] else (g[4], g[3], g[5]))
        number = _MONTHS.get(month.lower().rstrip(".")) or next(
            v for k, v in _MONTHS.items() if k.startswith(month.lower()[:3]))
        iso = f"{int(year):04d}-{number:02d}-{int(day):02d}"
        add(match.group(0), iso, "date", match.start(), match.end())
    # 1d. Periods: 'March 2024', 'the first quarter of 2024', 'the last 30 days'.
    for start, end, text, value in periods(question):
        if free(start, end):
            add(text, value, "period", start, end)
    for match in _LETTER.finditer(question):
        if free(match.start(1), match.end(1)):
            add(match.group(1), match.group(1), "text", match.start(1), match.end(1))

    # 2. Longest stored-value matches over token n-grams.
    spans = []
    for i in range(len(tokens)):
        for j in range(min(len(tokens), i + MAX_NGRAM), i, -1):
            start, end = tokens[i].start, tokens[j - 1].end
            surface = question[start:end]
            norm = _normalize(surface)
            hits = index.exact.get(norm) or ()
            if not hits and question[end:end + 2] in ("'s", "’s"):
                # A stored value with a possessive ("McDonald's"): the tokenizer splits it off.
                hits = index.exact.get(_normalize(question[start:end + 2])) or ()
                if hits:
                    end += 2
                    surface = question[start:end]
            if not hits and norm.endswith("s") and len(norm) > 3:
                # 'goalies' for a stored 'goalie', 'countries' for 'country'.
                for singular in _singulars(norm):
                    hits = index.exact.get(singular) or ()
                    if hits:
                        break
            if not hits:
                continue
            words = [t.lower for t in tokens[i:j]]
            if all(w in STOPWORDS for w in words) and not all(t.text.isupper() for t in tokens[i:j]):
                continue
            if j - i == 1:
                word = tokens[i].text
                # Short or ordinary words only count when written like a value, or right
                # after the name of the column that stores them ('with code x').
                in_name = i > 0 and word[:1].isupper() and any(
                    0 <= n < len(tokens) and tokens[n].text[:1].isupper() for n in (i - 1, i + 1))
                if len(word) <= 2 and not word.isupper() and not word.isdigit() and not in_name and not (
                        i > 0 and any(schema.column(k).label.split()[-1] == lemma(tokens[i - 1].text)
                                      for k, _ in hits)):
                    continue
                if (word.islower() and lemma(word) in schema_words and not word.isdigit()):
                    continue
                if word.isdigit():
                    continue  # numbers are handled below, with value hits attached
                if word.lower() in NUMBER_WORDS and not any(str(v) == word for _, v in hits):
                    continue  # 'two or more regions' is a number, not the storm named 'Two'
            spans.append((j - i, start, end, surface, hits))
            break
    for _, start, end, surface, hits in sorted(spans, key=lambda s: (-s[0], s[1])):
        if free(start, end):
            add(surface, hits[0][1], "text", start, end, hits, exact=True)

    # 3. Numbers (digits and number words).
    for match in _NUMBER.finditer(question):
        if not free(match.start(), match.end()):
            continue
        text = match.group(0)
        matches = index.exact.get(_normalize(text), ())
        add(text, _parse_number(text), "number", match.start(), match.end(), matches,
            exact=bool(matches))
    for token in tokens:
        word_value = NUMBER_WORDS.get(token.lower)
        if word_value is None or not free(token.start, token.end):
            continue
        if token.lower == "one" and _one_is_pronoun(question, token):
            continue
        add(token.text, word_value, "number", token.start, token.end)
    for position, token in enumerate(tokens):
        ordinal = ORDINALS.get(token.lower)
        if ordinal is None or not free(token.start, token.end) or token.lower == "first":
            continue
        following = tokens[position + 1].lower if position + 1 < len(tokens) else ""
        if following.endswith("est") or following in ("most", "least", "highest", "lowest"):
            continue      # 'second highest' is a rank offset, not a value
        add(token.text, ordinal, "number", token.start, token.end)

    # 4. Capitalized spans not found verbatim: fuzzy candidates (typos, case).
    for start, end, surface in _capitalized_spans(question, tokens):
        if not free(start, end) or surface.lower() in STOPWORDS:
            continue
        if lemma(surface.lower()) in schema_words and " " not in surface:
            continue
        fuzzy = _fuzzy(index, surface) or _probe_large_columns(schema, surface)
        parts = _name_parts(index, question, start, end) if not fuzzy else None
        if parts:
            # 'Linda Smith' stored as first name 'Linda' and last name 'Smith'.
            for part_start, part_end, matches in parts:
                part = question[part_start:part_end]
                add(part, matches[0][1], "text", part_start, part_end, matches, exact=True)
        elif fuzzy:
            add(surface, fuzzy[0][1], "text", start, end, fuzzy)
        elif not _names_schema(surface, schema_words):
            # A proper noun the database does not contain ('from Brazil') is still a
            # condition: dropping it would silently widen the answer.
            add(surface, surface, "text", start, end)

    found.sort(key=lambda lit: lit.start)
    return [Literal(f"L{i + 1}", *(_fields(lit)[1:])) for i, lit in enumerate(found)]


_MONTH_YEAR = re.compile(rf"\b{_MONTH_RE}\s+(?:of\s+)?(\d{{4}})\b", re.I)
_QUARTER = re.compile(r"\b(?:the\s+)?(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s+(?:of\s+)?(\d{4})\b|"
                      r"\bQ([1-4])\s*(?:of\s+)?(\d{4})\b", re.I)
_HALF = re.compile(r"\b(?:the\s+)?(first|second|1st|2nd)\s+half\s+(?:of\s+)?(\d{4})\b", re.I)
_RELATIVE = re.compile(
    r"\b(?:in\s+)?(?:the\s+)?(?:(?:last|past|previous)\s+(?P<n>\d+|two|three|four|five|six|seven|ten|twelve|"
    r"thirty)\s+(?P<unit>days?|weeks?|months?|years?)|past\s+(?P<punit>day|week|month|year))\b|"
    r"\b(?P<which>this|last|current|previous)\s+(?P<cal>week|month|year)\b|\b(?P<day>today|yesterday)\b", re.I)
_ORDINAL_INDEX = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    return date(day.year + month // 12, month % 12 + 1, 1)


def periods(question: str, today: date | None = None) -> list[tuple[int, int, str, tuple[str, str, str]]]:
    """Date periods as half-open ranges: (start, end, surface, (first day, day after, label)).

    Relative periods are resolved against today's date and say so in their label, so
    the interpretation shown to the user states which dates were used.
    """
    today = today or date.today()
    found = []
    for m in _QUARTER.finditer(question):
        q = _ORDINAL_INDEX.get((m.group(1) or "").lower()) or int(m.group(3))
        year = int(m.group(2) or m.group(4))
        first = date(year, 3 * (q - 1) + 1, 1)
        found.append((m.start(), m.end(), m.group(0), (first, _add_months(first, 3), f"Q{q} {year}")))
    for m in _HALF.finditer(question):
        h = _ORDINAL_INDEX[m.group(1).lower()]
        first = date(int(m.group(2)), 6 * (h - 1) + 1, 1)
        found.append((m.start(), m.end(), m.group(0), (first, _add_months(first, 6),
                                                       f"the {'first' if h == 1 else 'second'} half of {first.year}")))
    for m in _MONTH_YEAR.finditer(question):
        name = m.group(1).lower().rstrip(".")
        number = _MONTHS.get(name) or next(v for k, v in _MONTHS.items() if k.startswith(name[:3]))
        first = date(int(m.group(2)), number, 1)
        found.append((m.start(), m.end(), m.group(0), (first, _add_months(first, 1), first.strftime("%B %Y"))))
    for m in _RELATIVE.finditer(question):
        if m.group("day"):
            first = today if m.group("day").lower() == "today" else today - timedelta(days=1)
            end = first + timedelta(days=1)
            label = m.group("day").lower()
        elif m.group("which"):
            which, unit = m.group("which").lower(), m.group("cal").lower()
            back = which in ("last", "previous")
            if unit == "year":
                first = date(today.year - back, 1, 1)
                end = date(first.year + 1, 1, 1)
            elif unit == "month":
                first = _add_months(date(today.year, today.month, 1), -back)
                end = _add_months(first, 1)
            else:
                monday = today - timedelta(days=today.weekday())
                first = monday - timedelta(days=7 * back)
                end = first + timedelta(days=7)
            label = f"{which} {unit}"
        else:
            count_text = (m.group("n") or "1").lower()
            count = int(count_text) if count_text.isdigit() else NUMBER_WORDS.get(count_text, 1)
            unit = (m.group("unit") or m.group("punit")).lower().rstrip("s")
            days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * count
            first, end = today - timedelta(days=days), today + timedelta(days=1)
            label = f"the last {count} {unit}s" if count != 1 else f"the past {unit}"
        found.append((m.start(), m.end(), m.group(0),
                      (first, end, f"{label} (relative to today, {today.isoformat()})")))
    return [(a, b, text, (first.isoformat(), end.isoformat(), label))
            for a, b, text, (first, end, label) in found]


def _fields(lit: Literal) -> tuple:
    return (lit.id, lit.text, lit.value, lit.kind, lit.start, lit.end, lit.columns, lit.exact,
            lit.quoted, lit.alternatives)


def _name_parts(index: ValueIndex, question: str, start: int, end: int
                ) -> list[tuple[int, int, tuple[tuple[str, str], ...]]] | None:
    """Each word of a multi-word span found in a different column of one table."""
    words = [(m.start() + start, m.end() + start) for m in re.finditer(r"\S+", question[start:end])]
    if len(words) < 2:
        return None
    found = []
    for a, b in words:
        matches = index.exact.get(_normalize(question[a:b]))
        if not matches:
            return None
        found.append((a, b, matches))
    for table in {key.split(".")[0] for key, _ in found[0][2]}:
        columns = [{k for k, _ in m if k.split(".")[0] == table} for _, _, m in found]
        if not all(columns):
            continue
        for assignment in product(*[sorted(c) for c in columns]):
            if len(set(assignment)) == len(assignment):       # one column per word
                return [(a, b, tuple(m for m in matches if m[0] == column))
                        for (a, b, matches), column in zip(found, assignment)]
    return None


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


def _names_schema(surface: str, schema_words: set[str]) -> bool:
    words = [lemma(w) for w in surface.lower().split()]
    return all(w in schema_words or w in STOPWORDS for w in words)


DEMONYMS = {
    "french": "france", "spanish": "spain", "dutch": "netherlands", "british": "uk", "english": "england",
    "swiss": "switzerland", "greek": "greece", "turkish": "turkey", "polish": "poland",
    "danish": "denmark", "swedish": "sweden", "finnish": "finland", "irish": "ireland",
    "welsh": "wales", "scottish": "scotland", "american": "usa", "norwegian": "norway",
    "portuguese": "portugal", "belgian": "belgium", "thai": "thailand", "filipino": "philippines",
}


def _demonym(index: ValueIndex, norm: str) -> list[tuple[str, str]]:
    """'Japanese' -> 'Japan', 'German' -> 'Germany', 'French' -> 'France'."""
    country = DEMONYMS.get(norm)
    if country:
        for value in (country, {"usa": "united states", "uk": "united kingdom"}.get(country, "")):
            if value in index.exact:
                return list(index.exact[value])
    out: list[tuple[str, str]] = []
    for value, matches in index.exact.items():
        if " " in value or len(value) < 4:
            continue
        shorter = min(len(value), len(norm))
        prefix = len(_common_prefix(value, norm))
        if prefix >= 4 and prefix >= 0.8 * shorter and abs(len(value) - len(norm)) <= 4 and (
                norm.endswith(("ese", "ian", "an", "ish", "i")) or value.endswith(("y", "a"))):
            out.extend(matches)
    return out[:6]


def _common_prefix(a: str, b: str) -> str:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return a[:i]


def _fuzzy(index: ValueIndex, surface: str) -> list[tuple[str, str]]:
    norm = _normalize(surface)
    if len(norm) < 4:
        return []
    pool = [v for v in index.exact if abs(len(v) - len(norm)) <= max(2, len(norm) // 4)]
    close = difflib.get_close_matches(norm, pool, n=3, cutoff=0.86)
    out: list[tuple[str, str]] = []
    for value in close:
        out.extend(index.exact[value])
    if not out and " " not in norm:
        out = _demonym(index, norm)
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
    for i, word in enumerate(lemmas):
        positions.setdefault(word, tokens[i].start)
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
    named = dict(tables)
    literals = extract_literals(question, schema)     # stored values: the shared base schema
    measures = derive(question, schema, columns, mention)
    if measures:
        schema = schema.with_columns(measures)
        for measure in measures:
            assert measure.formula is not None
            _, left, right = measure.formula
            columns[measure.key] = 0.9
            positions = [mention[k] for k in (left, right) if k in mention]
            if positions:
                mention[measure.key] = min(positions)
    for literal in literals:
        for key in literal.columns:
            columns[key] = max(columns.get(key, 0.0), 0.9)
            table = key.split(".", 1)[0]
            tables[table] = max(tables.get(table, 0.0), 0.5)
            mention.setdefault(key, literal.start)
    return Grounding(question, schema, tokenize(question), literals, columns, tables, mention, named)
