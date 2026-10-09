"""Offline test doubles: a scripted Jev and a small relational fixture database."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from intentsql.jev import Answer, Exchange, Question, Usage

SHOP_DDL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT, vip TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, category TEXT, price REAL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
                     placed TEXT, status TEXT, total REAL);
CREATE TABLE order_items (order_id INTEGER REFERENCES orders(id),
                          product_id INTEGER REFERENCES products(id), quantity INTEGER,
                          PRIMARY KEY (order_id, product_id));
CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, dept TEXT, salary REAL,
                        manager_id INTEGER);
INSERT INTO customers VALUES (1,'Ann','Paris','Y'),(2,'Bob','Lyon','N'),(3,'Cy','Paris','N'),
                             (4,'Di','Nice','N'),(5,'Ed','Lyon','Y');
INSERT INTO products VALUES (1,'Latte','coffee',3.5),(2,'Mocha','coffee',4.0),
                            (3,'Green','tea',2.5),(4,'Scone','food',3.0);
INSERT INTO orders VALUES (1,1,'2024-01-05','paid',10),(2,1,'2024-02-10','paid',20),
                          (3,2,'2024-02-11','open',5),(4,3,'2024-03-01','paid',7.5),
                          (5,5,'2024-03-09','cancelled',12);
INSERT INTO order_items VALUES (1,1,2),(1,4,1),(2,2,3),(3,3,1),(4,1,1),(4,2,1),(5,4,4);
INSERT INTO employees VALUES (1,'Zoe','sales',5000,NULL),(2,'Yan','sales',4000,1),
                             (3,'Xia','tech',7000,NULL),(4,'Wim','tech',6500,3),(5,'Val','tech',3000,3);
"""


def make_shop(directory: str | Path) -> Path:
    path = Path(directory) / "shop.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(SHOP_DDL)
    return path


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    return str(value)


Rule = tuple[str, str, Any]   # (question-key regex, instructions regex, answer spec)


class ScriptedJev:
    """Answers questions from rules matched on the question key and text.

    The answer spec is, for a Choice, a regex matched against option *texts*
    (or an option key prefixed with '='); for a Noul, a probability.  Choices
    without a matching rule pick a "no/none/unordered/all" option when one
    exists; Nouls default to 0.
    """

    configured = True

    def __init__(self, rules: list[Rule] | None = None,
                 fallback: Callable[[str, Question], Any] | None = None) -> None:
        self.rules = rules or []
        self.fallback = fallback
        self.asked: list[dict[str, Question]] = []

    def answer(self, key: str, question: Question) -> Answer:
        text = _text(question.instructions)
        for key_pattern, text_pattern, spec in self.rules:
            if re.search(key_pattern, key) and re.search(text_pattern, text, re.I):
                return self._apply(question, spec)
        if self.fallback:
            spec = self.fallback(key, question)
            if spec is not None:
                return self._apply(question, spec)
        if question.kind == "noul":
            return Answer("noul", p=0.0)
        options = question.options or {}
        for default in ("none", "no", "unordered", "all", "other"):
            if default in options:
                return self._choice(options, default)
        if key.startswith("role_"):
            return self._choice(options, "value")
        return self._choice(options, next(iter(options)))

    def _apply(self, question: Question, spec: Any) -> Answer:
        if question.kind == "noul":
            return Answer("noul", p=float(spec))
        options = question.options or {}
        if isinstance(spec, str) and spec.startswith("="):
            return self._choice(options, spec[1:])
        for key, text in options.items():
            if re.search(spec, _text(text), re.I):
                return self._choice(options, key)
        raise AssertionError(f"No option matches {spec!r}: {list(options.values())[:8]}")

    @staticmethod
    def _choice(options: dict[str, Any], key: str) -> Answer:
        probabilities = {k: (0.97 if k == key else 0.03 / max(1, len(options) - 1)) for k in options}
        return Answer("choice", key, probabilities, 0.95)

    def ask(self, state: Any, questions: dict[str, Question], *, usage: Usage,
            log: list[Exchange] | None = None, purpose: str = "") -> dict[str, Answer]:
        self.asked.append(questions)
        usage.calls += 1
        usage.rounds += 1
        answers = {key: self.answer(key, q) for key, q in questions.items()}
        if log is not None:
            log.append(Exchange(purpose, state, {k: q.payload() for k, q in questions.items()},
                                {k: a.__dict__ for k, a in answers.items()}, 0, 0, 0, "fake"))
        return answers
