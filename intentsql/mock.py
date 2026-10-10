"""Deterministic offline stand-in for Jev (provider ``mock``).

It answers every bounded question with a fixed, conservative default: "none"
/ "no" style options where a question offers them, the first option
otherwise, and a confident "yes" for verification.  It needs no network and
no key, so the application, the API and the tests run offline.  Its answers
reflect no understanding of the request: use it to exercise the software, never
to measure accuracy.  Rules can be scripted for tests (see tests/fakes.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from intentsql.jev import Answer, Exchange, Question, Usage
from intentsql.providers import PROVIDERS

DEFAULT_CHOICES = ("none", "no", "unordered", "all", "other")


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    return str(value)


Rule = tuple[str, str, Any]   # (question-key regex, instructions regex, answer spec)


class MockClient:
    """Answers from rules matched on the question key and text, else from defaults.

    A rule's answer spec is, for a Choice, a regex matched against option *texts*
    (or an option key prefixed with '='); for a Noul, a probability; for a Score,
    a level number.
    """

    configured = True

    def __init__(self, rules: list[Rule] | None = None,
                 fallback: Callable[[str, Question], Any] | None = None) -> None:
        self.rules = rules or []
        self.fallback = fallback
        self.asked: list[dict[str, Question]] = []
        self.provider = PROVIDERS["mock"]
        self.model = "mock"

    def describe(self) -> dict[str, Any]:
        return {"provider": "mock", "label": self.provider.label, "url": "", "model": "mock",
                "has_key": False, "budget_usd": None, "spent_usd": 0.0, "requests": 0}

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
            return Answer("noul", p=0.95 if key.startswith("verify") else 0.0)
        if question.kind == "score":
            return Answer("score", probabilities={"0": 1.0}, confidence=1.0, score=0.0)
        options = question.options or {}
        if key == "pick":
            return self._choice(options, "A")
        if key == "scope":
            return self._choice(options, "read")
        for default in DEFAULT_CHOICES:
            if default in options:
                return self._choice(options, default)
        if key.startswith("role_"):
            return self._choice(options, "value")
        return self._choice(options, next(iter(options)))

    def _apply(self, question: Question, spec: Any) -> Answer:
        if question.kind == "noul":
            return Answer("noul", p=float(spec))
        if question.kind == "score":
            return Answer("score", probabilities={str(int(spec)): 1.0}, confidence=1.0, score=float(spec))
        options = question.options or {}
        if isinstance(spec, str) and spec.startswith("="):
            return self._choice(options, spec[1:])
        for key, text in options.items():
            if re.search(spec, _text(text), re.I):
                return self._choice(options, key)
        raise AssertionError(f"No option matches {spec!r}: {list(options.values())[:8]}")

    @staticmethod
    def _choice(options: Mapping[str, Any], key: str) -> Answer:
        probabilities = {k: (0.97 if k == key else 0.03 / max(1, len(options) - 1)) for k in options}
        return Answer("choice", key, probabilities, 0.95)

    def ask(self, state: Any, questions: dict[str, Question], *, usage: Usage,
            log: list[Exchange] | None = None, purpose: str = "") -> dict[str, Answer]:
        self.asked.append(questions)
        usage.calls += 1
        usage.rounds += 1
        usage.provider = "mock"
        answers = {key: self.answer(key, q) for key, q in questions.items()}
        if log is not None:
            log.append(Exchange(purpose, state, {k: q.payload() for k, q in questions.items()},
                                {k: a.__dict__ for k, a in answers.items()}, 0, 0, 0, "mock",
                                provider="mock"))
        return answers
