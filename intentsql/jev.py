"""Jev (System One) client: typed questions, validated answers, accounting, provenance.

Design rules:

* Every question is either a ``choice`` over options supplied by code or a
  ``noul`` (probability of yes).  Answers are validated against the options;
  an out-of-set answer is an error, never silently accepted.
* Large fan-outs are split into several requests that run concurrently, so a
  round of decisions costs one network round trip of latency.
* Usage (calls, tokens, latency, retries) is accounted per ``Session`` so a
  single request's cost is reported exactly, even when the client is shared.
* The API key is read from configuration and only ever placed in the HTTP
  ``Authorization`` header of its own provider; it never appears in logs,
  traces or exchanges.
* Every exchange records the provider and the model snapshot that answered it.
  There is no fallback between providers (see providers.py).
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
MAX_QUESTIONS_PER_REQUEST = 160
MAX_CHARS_PER_REQUEST = 90_000          # default; providers set their own (context windows differ)


class JevError(RuntimeError):
    """Transport or protocol failure talking to Jev."""


@dataclass(frozen=True)
class Question:
    kind: str                         # "choice" | "noul" | "score"
    instructions: Any
    options: Mapping[str, Any] | None = None   # choice: key -> description
    true: Any = None                  # noul criteria
    false: Any = None
    levels: tuple[Any, ...] = ()      # score: ordered level descriptions (level 0 first)

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.kind, "instructions": self.instructions}
        if self.kind == "choice":
            body["criteria"] = dict(self.options or {})
        elif self.kind == "score":
            body["criteria"] = list(self.levels)
        elif self.true is not None or self.false is not None:
            body["criteria"] = {k: v for k, v in (("true", self.true), ("false", self.false))
                                if v is not None}
        return body


def choice(instructions: Any, options: Mapping[str, Any]) -> Question:
    if not 2 <= len(options) <= 255:
        raise ValueError(f"A choice needs 2-255 options, got {len(options)}")
    return Question("choice", instructions, dict(options))


def noul(instructions: Any, true: Any = None, false: Any = None) -> Question:
    return Question("noul", instructions, None, true, false)


def score(instructions: Any, levels: list[Any] | tuple[Any, ...]) -> Question:
    if not 2 <= len(levels) <= 10:
        raise ValueError(f"A score needs 2-10 levels, got {len(levels)}")
    return Question("score", instructions, levels=tuple(levels))


@dataclass(frozen=True)
class Answer:
    kind: str
    choice: str | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    confidence: float | None = None
    p: float | None = None            # noul probability of "yes"
    score: float | None = None        # score: probability-weighted level (0 = first level)

    @property
    def top_probability(self) -> float:
        if self.kind == "noul":
            return max(self.p or 0.0, 1 - (self.p or 0.0))
        return max(self.probabilities.values()) if self.probabilities else 0.0

    @property
    def picked(self) -> str:
        """The chosen option key of a Choice (validated answers always have one)."""
        if self.choice is None:
            raise JevError(f"A {self.kind} answer has no choice")
        return self.choice

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])

    def margin(self) -> float:
        ranked = self.ranked()
        if len(ranked) < 2:
            return 1.0
        return ranked[0][1] - ranked[1][1]


@dataclass
class Exchange:
    purpose: str
    state: Any
    questions: dict[str, dict[str, Any]]
    answers: dict[str, Any]
    input_tokens: int
    output_tokens: int
    latency_ms: int
    model: str | None
    cached: bool = False
    provider: str = ""
    cost_usd: float | None = None     # as reported by the provider (OpenRouter)
    request_id: str | None = None


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    rounds: int = 0
    latency_ms: int = 0
    cached_calls: int = 0
    cost_usd: float = 0.0
    provider: str = ""
    models: list[str] = field(default_factory=list)   # model snapshots that answered

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "cost_usd": round(self.cost_usd, 8), "models": list(self.models)}


def _validate(key: str, question: Question, raw: Any) -> Answer:
    if not isinstance(raw, dict):
        raise JevError(f"Answer {key} is not an object")
    if question.kind == "noul":
        p = raw.get("noul")
        if not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise JevError(f"Answer {key} has no valid noul probability")
        return Answer("noul", p=float(p))
    if question.kind == "score":
        value = raw.get("score")
        top = len(question.levels) - 1
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not -0.01 <= value <= top + 0.01:
            raise JevError(f"Answer {key} has no valid score")
        levels = {str(i) for i in range(top + 1)}
        probabilities = {str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()
                         if str(k) in levels and isinstance(v, (int, float)) and math.isfinite(v)}
        return Answer("score", probabilities=probabilities, confidence=raw.get("confidence"),
                      score=float(value))
    selected = raw.get("choice")
    options = question.options or {}
    if selected not in options:
        raise JevError(f"Answer {key} selected {selected!r}, outside the supplied options")
    probabilities = raw.get("probabilities") or {}
    clean = {k: float(v) for k, v in probabilities.items()
             if k in options and isinstance(v, (int, float)) and math.isfinite(v)}
    if not clean:
        clean = {selected: 1.0}
    return Answer("choice", selected, clean, raw.get("confidence"))


class BudgetExceeded(JevError):
    """The configured spending limit has been reached; no further requests are sent."""


class _Budget:
    """Process-wide cost accounting against an optional limit (INTENTSQL_BUDGET_USD)."""

    def __init__(self, limit: float | None) -> None:
        self.limit = limit
        self.spent = 0.0
        self.requests = 0
        self.lock = threading.Lock()

    def check(self) -> None:
        with self.lock:
            if self.limit is not None and self.spent >= self.limit:
                raise BudgetExceeded(f"The spending limit of ${self.limit:.2f} has been reached "
                                     f"(${self.spent:.4f} spent); no further Jev requests are sent.")

    def add(self, cost: float | None) -> None:
        with self.lock:
            self.requests += 1
            self.spent += cost or 0.0


class JevClient:
    """Thread-safe HTTP client for one provider, with keep-alive, retries and an optional cache."""

    def __init__(self, url: str | None = None, model: str | None = None,
                 api_key: str | None = None, *, provider: str | None = None, timeout: float = 45.0,
                 max_retries: int = 3, cache_dir: str | Path | None = None,
                 max_parallel: int = 8, budget_usd: float | None = None) -> None:
        from intentsql.config import settings
        from intentsql.providers import PROVIDERS
        cfg = settings()
        self.provider = PROVIDERS[provider or cfg.provider]
        same = self.provider.name == cfg.provider
        self.url = url or (cfg.jev_url if same else self.provider.url)
        self.model = model or (cfg.jev_model if same else self.provider.model)
        self._api_key = api_key if api_key is not None else (cfg.jev_api_key if same
                                                             else cfg.key_for(self.provider.name))
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_chars = min(MAX_CHARS_PER_REQUEST, self.provider.max_chars_per_request)
        cache = cache_dir if cache_dir is not None else cfg.jev_cache_dir
        # The cache is per provider and model: answers from one never stand in for another.
        self.cache_dir = (Path(cache) / self.provider.name / self.model.replace("/", "_")
                          if cache else None)
        self.budget = _Budget(budget_usd if budget_usd is not None else cfg.budget_usd)
        self._http = httpx.Client(timeout=timeout, http2=False,
                                  limits=httpx.Limits(max_keepalive_connections=16))
        self._pool = ThreadPoolExecutor(max_workers=max_parallel)

    @property
    def configured(self) -> bool:
        return bool(self._api_key) or "127.0.0.1" in self.url or "localhost" in self.url

    def describe(self) -> dict[str, Any]:
        """Provider details safe to show (never the key)."""
        return {"provider": self.provider.name, "label": self.provider.label, "url": self.url,
                "model": self.model, "has_key": bool(self._api_key),
                "budget_usd": self.budget.limit, "spent_usd": round(self.budget.spent, 6),
                "requests": self.budget.requests}

    def close(self) -> None:
        self._http.close()
        self._pool.shutdown(wait=False)

    # -- transport ---------------------------------------------------------------
    def _cache_path(self, body: dict[str, Any]) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)
                                .encode("utf-8")).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def _error(self, status: int) -> str:
        name = self.provider.label
        return {401: f"{name} rejected the API key. Check the provider settings.",
                402: f"{name}: the account has no credits left. {self.provider.billing_hint}",
                403: f"{name} denied access for this key or model.",
                404: f"{name} does not know this endpoint or model ({self.model}).",
                422: f"{name} rejected the request format.",
                429: f"{name} rate limit reached; try again shortly."}.get(
            status, f"{name} returned HTTP {status}.")

    def _post(self, body: dict[str, Any], usage: Usage, lock: threading.Lock) -> tuple[dict, bool]:
        cache = self._cache_path(body)
        if cache and cache.is_file():
            try:
                return json.loads(cache.read_text(encoding="utf-8")), True
            except (OSError, ValueError):
                pass
        self.budget.check()
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "IntentSQL/1.0"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        attempt = 0
        name = self.provider.label
        while True:
            attempt += 1
            try:
                response = self._http.post(self.url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                if attempt <= self.max_retries:
                    with lock:
                        usage.retries += 1
                    continue
                raise JevError(f"{name} did not respond in time.") from exc
            except httpx.HTTPError as exc:
                if attempt <= self.max_retries:
                    with lock:
                        usage.retries += 1
                    time.sleep(0.3 * attempt)
                    continue
                raise JevError(f"Could not reach {name}. Check the network.") from exc
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise JevError(f"{name} returned invalid JSON.") from exc
                reported = (data.get("usage") or {}) if isinstance(data, dict) else {}
                self.budget.add(_number(reported.get("cost")))
                if cache:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache.with_suffix(".tmp")
                    tmp.write_text(json.dumps(data), encoding="utf-8")
                    tmp.replace(cache)
                return data, False
            if response.status_code in RETRYABLE and attempt <= self.max_retries:
                with lock:
                    usage.retries += 1
                retry_after = response.headers.get("retry-after")
                try:
                    delay = min(8.0, float(retry_after)) if retry_after else 0.5 * 2 ** (attempt - 1)
                except ValueError:
                    delay = 0.5 * 2 ** (attempt - 1)
                time.sleep(delay)
                continue
            raise JevError(self._error(response.status_code))

    # -- public ------------------------------------------------------------------
    def ask(self, state: Any, questions: Mapping[str, Question], *, usage: Usage,
            log: list[Exchange] | None = None, purpose: str = "") -> dict[str, Answer]:
        """Ask all questions about ``state`` in as few concurrent requests as possible."""
        if not questions:
            return {}
        if not self.configured:
            variables = " or ".join(self.provider.key_variables) or "a key"
            raise JevError(f"No API key configured for {self.provider.label}. Set {variables}.")
        chunks = _chunk(questions, self.max_chars)
        lock = threading.Lock()
        started = time.monotonic()

        def run(chunk: dict[str, Question]) -> tuple[dict[str, Answer], Exchange]:
            body: dict[str, Any] = {"model": self.model, "state": state,
                                    "questions": {k: q.payload() for k, q in chunk.items()}}
            t0 = time.monotonic()
            data, cached = self._post(body, usage, lock)
            raw_answers = data.get("answers") if isinstance(data, dict) else None
            if not isinstance(raw_answers, dict):
                raise JevError(f"{self.provider.label} returned no answers object.")
            answers = {}
            for key, question in chunk.items():
                if key not in raw_answers:
                    raise JevError(f"{self.provider.label} omitted answer {key}.")
                answers[key] = _validate(key, question, raw_answers[key])
            reported = data.get("usage") or {}
            exchange = Exchange(purpose, state, body["questions"],
                                {k: raw_answers[k] for k in chunk},
                                int(reported.get("input_tokens") or 0),
                                int(reported.get("output_tokens") or 0),
                                round((time.monotonic() - t0) * 1000),
                                data.get("model"), cached, self.provider.name,
                                _number(reported.get("cost")), data.get("id"))
            return answers, exchange

        if len(chunks) == 1:
            results = [run(chunks[0])]
        else:
            results = list(self._pool.map(run, chunks))
        merged: dict[str, Answer] = {}
        with lock:
            usage.rounds += 1
            usage.provider = self.provider.name
            usage.latency_ms += round((time.monotonic() - started) * 1000)
            for answers, exchange in results:
                merged.update(answers)
                usage.calls += 1
                usage.cached_calls += exchange.cached
                usage.input_tokens += exchange.input_tokens
                usage.output_tokens += exchange.output_tokens
                if not exchange.cached:
                    usage.cost_usd += exchange.cost_usd or 0.0
                if exchange.model and exchange.model not in usage.models:
                    usage.models.append(exchange.model)
                if log is not None:
                    log.append(exchange)
        return merged


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _chunk(questions: Mapping[str, Question], max_chars: int = MAX_CHARS_PER_REQUEST
           ) -> list[dict[str, Question]]:
    chunks: list[dict[str, Question]] = [{}]
    size = 0
    for key, question in questions.items():
        cost = len(json.dumps(question.payload(), ensure_ascii=False))
        if chunks[-1] and (len(chunks[-1]) >= MAX_QUESTIONS_PER_REQUEST
                           or size + cost > max_chars):
            chunks.append({})
            size = 0
        chunks[-1][key] = question
        size += cost
    return chunks


_SHARED: Any = None
_SHARED_LOCK = threading.Lock()


def shared_client() -> JevClient:
    """The client for the configured provider (the offline mock when that is selected)."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            from intentsql.config import settings
            if settings().provider == "mock":
                from intentsql.mock import MockClient
                _SHARED = MockClient()
            else:
                _SHARED = JevClient()
        return _SHARED


def reset_shared_client() -> None:
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is not None:
            _SHARED.close()
        _SHARED = None
