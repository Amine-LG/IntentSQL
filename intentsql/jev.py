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
FATAL_STATUS = {401, 402, 403, 404, 422}
MAX_QUESTIONS_PER_REQUEST = 160
MAX_CHARS_PER_REQUEST = 90_000          # default; providers set their own (context windows differ)


class JevError(RuntimeError):
    """Transport or protocol failure talking to Jev.

    ``fatal`` errors (bad key, no credits, spending limit, wrong endpoint, a provider that
    stays unreachable after retries) will fail every request; batch evaluations stop on them
    instead of recording a run of errors as results."""

    def __init__(self, message: str, *, fatal: bool = False, kind: str = "error") -> None:
        super().__init__(message)
        self.fatal = fatal
        # auth | credits | budget | unavailable | request | not_configured | protocol | unsupported | error
        self.kind = kind


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
    cost_usd: float | None = None     # when the provider reports a cost
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
        raise JevError(f"answer {key} is not an object", kind="protocol")
    if question.kind == "noul":
        p = raw.get("noul")
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise JevError(f"answer {key} has no 'noul' probability between 0 and 1", kind="protocol")
        return Answer("noul", p=float(p))
    if question.kind == "score":
        value = raw.get("score")
        top = len(question.levels) - 1
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or not -0.01 <= value <= top + 0.01):
            raise JevError(f"answer {key} has no valid 'score'", kind="protocol")
        levels = {str(i) for i in range(top + 1)}
        probabilities = {str(k): float(v) for k, v in _mapping(raw.get("probabilities")).items()
                         if str(k) in levels and _probability(v)}
        return Answer("score", probabilities=probabilities, confidence=_number(raw.get("confidence")),
                      score=float(value))
    selected = raw.get("choice")
    options = question.options or {}
    if not isinstance(selected, str) or selected not in options:
        raise JevError(f"answer {key} has 'choice' {str(selected)[:40]!r}, which is not one of the "
                       "options offered", kind="protocol")
    clean = {k: float(v) for k, v in _mapping(raw.get("probabilities")).items()
             if k in options and _probability(v)}
    if not clean:
        clean = {selected: 1.0}
    return Answer("choice", selected, clean, _number(raw.get("confidence")))


def _mapping(value: Any) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _probability(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            and 0 <= value <= 1)


def connection_test_questions(kinds: tuple[str, ...]) -> dict[str, Question]:
    """One tiny question per decision type: the connection and compatibility check."""
    questions: dict[str, Question] = {}
    if "choice" in kinds:
        questions["team"] = choice("Which team should handle `ticket`?",
                                   {"billing": "the billing team", "shipping": "the shipping team"})
    if "noul" in kinds:
        questions["refund"] = noul("Does `ticket` ask for money back?")
    if "score" in kinds:
        questions["urgency"] = score("How urgent is `ticket`?", ["not urgent", "somewhat urgent", "very urgent"])
    return questions


TEST_STATE = {"ticket": "I was charged twice for my order. Please refund the extra charge."}


# ---- connection state (what the app shows; never 'connected' without a real answer) ----

FAILURE_STATES = {"auth": "auth_failed", "credits": "no_credits", "unavailable": "unavailable",
                  "request": "error", "not_configured": "not_configured", "budget": "budget_reached",
                  "protocol": "incompatible", "unsupported": "incompatible"}


@dataclass
class ConnectionState:
    state: str              # connected | auth_failed | no_credits | unavailable | budget_reached | error | ...
    message: str = ""
    model_snapshot: str | None = None
    checked_at: float = 0.0


_STATES: dict[tuple[str, str], ConnectionState] = {}
_STATES_LOCK = threading.Lock()


def record_state(provider: str, model: str, state: str, message: str = "",
                 snapshot: str | None = None) -> None:
    with _STATES_LOCK:
        _STATES[(provider, model)] = ConnectionState(state, message, snapshot, time.time())


def forget_state(provider: str) -> None:
    """Drop what is known about a provider (its settings changed)."""
    with _STATES_LOCK:
        for key in [k for k in _STATES if k[0] == provider]:
            del _STATES[key]


def connection_state(provider: str, model: str) -> ConnectionState | None:
    with _STATES_LOCK:
        return _STATES.get((provider, model))


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
                                     f"(${self.spent:.4f} spent); no further Jev requests are sent.",
                                     fatal=True, kind="budget")

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
        cfg = settings()
        self.provider = cfg.providers[provider or cfg.provider]
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
        # A custom endpoint may need no key; it says so (HTTP 401) if it does.
        return bool(self._api_key) or self.provider.custom or "127.0.0.1" in self.url or "localhost" in self.url

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
        if self.provider.custom:
            custom = {
                401: f"{name} requires an API key, or rejected the one saved.",
                404: f"{name}: nothing answers at this URL, or the endpoint does not know the model "
                     f"'{self.model}' (HTTP 404). Check the URL and the model ID.",
                405: f"{name}: this URL does not accept POST requests (HTTP 405). Is it the decision endpoint?",
                400: f"{name} rejected IntentSQL's request (HTTP 400). The endpoint may not implement "
                     "the System One decision protocol.",
                415: f"{name} does not accept JSON requests (HTTP 415).",
                422: f"{name} rejected the request format (HTTP 422). The endpoint may not implement "
                     "the System One decision protocol."}
            if status in custom:
                return custom[status]
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
                raise JevError(f"{name} did not respond in time.", fatal=True, kind="unavailable") from exc
            except httpx.InvalidURL as exc:
                raise JevError(f"{name}: the endpoint URL is not valid.", fatal=True, kind="unsupported"
                               if self.provider.custom else "request") from exc
            except httpx.HTTPError as exc:
                if attempt <= self.max_retries:
                    with lock:
                        usage.retries += 1
                    time.sleep(0.3 * attempt)
                    continue
                where = f" at {httpx.URL(self.url).host}" if self.provider.custom else ""
                raise JevError(f"Could not reach {name}{where}. Check the network"
                               + (" and the URL" if self.provider.custom else "") + ".", fatal=True,
                               kind="unavailable") from exc
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise JevError(f"{name} answered with something that is not JSON"
                                   + (" (is this a web page rather than the API endpoint?)"
                                      if "html" in response.headers.get("content-type", "") else "")
                                   + ".", kind="protocol") from exc
                reported = _mapping(data.get("usage")) if isinstance(data, dict) else {}
                cost = _number(reported.get("cost"))
                if cost is None and self.provider.price_per_mtok is not None:
                    tokens = _number(reported.get("input_tokens")) or 0.0
                    cost = tokens * self.provider.price_per_mtok / 1_000_000     # an estimate
                self.budget.add(cost)
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
            status = response.status_code
            raise JevError(self._error(status) + _detail(response, self._api_key),
                           fatal=status in FATAL_STATUS or status in RETRYABLE
                           or (self.provider.custom and status in (400, 405, 415)),
                           kind={401: "auth", 403: "auth", 402: "credits", 404: "request", 422: "request",
                                 400: "protocol" if self.provider.custom else "error",
                                 415: "protocol" if self.provider.custom else "error",
                                 405: "request"}.get(
                               status, "unavailable" if status in RETRYABLE else "error"))

    # -- public ------------------------------------------------------------------
    def ask(self, state: Any, questions: Mapping[str, Question], *, usage: Usage,
            log: list[Exchange] | None = None, purpose: str = "") -> dict[str, Answer]:
        """Ask all questions about ``state`` in as few concurrent requests as possible.

        Every real outcome updates the provider's connection state shown in the app:
        'connected' only after a successful answer, the actual failure otherwise."""
        if not questions:
            return {}
        real_before = usage.calls - usage.cached_calls
        try:
            answers = self._ask(state, questions, usage=usage, log=log, purpose=purpose)
        except JevError as exc:
            # One malformed answer from a built-in provider is an error, not a verdict on it.
            if exc.kind in FAILURE_STATES and (self.provider.custom or exc.kind != "protocol"):
                record_state(self.provider.name, self.model, FAILURE_STATES[exc.kind], str(exc))
            raise
        if usage.calls - usage.cached_calls > real_before:          # a real (uncached) answer
            record_state(self.provider.name, self.model, "connected", "", usage.models[-1] if usage.models else None)
        return answers

    def _ask(self, state: Any, questions: Mapping[str, Question], *, usage: Usage,
             log: list[Exchange] | None = None, purpose: str = "") -> dict[str, Answer]:
        if not self.configured:
            variables = " or ".join(self.provider.key_variables) or "a key"
            raise JevError(f"No API key is set for {self.provider.label}. Add one in Settings "
                           f"or set {variables}.", fatal=True, kind="not_configured")
        if self.provider.problem:
            raise JevError(f"{self.provider.label}: {self.provider.problem}", fatal=True, kind="unsupported")
        unsupported = sorted({q.kind for q in questions.values()} - set(self.provider.kinds))
        if self.provider.custom and unsupported:
            raise JevError(f"{self.provider.label} is set up without {' and '.join(k.title() for k in unsupported)} "
                           "questions, which IntentSQL needs here.", fatal=True, kind="unsupported")
        chunks = _chunk(questions, self.max_chars)
        lock = threading.Lock()
        started = time.monotonic()

        def run(chunk: dict[str, Question]) -> tuple[dict[str, Answer], Exchange]:
            body: dict[str, Any] = {"model": self.model, "state": state,
                                    "questions": {k: q.payload() for k, q in chunk.items()}}
            t0 = time.monotonic()
            data, cached = self._post(body, usage, lock)
            raw_answers = data.get("answers") if isinstance(data, dict) else None
            try:
                if not isinstance(raw_answers, dict):
                    shape = (f"top-level fields: {', '.join(list(data)[:5])}" if isinstance(data, dict) and data
                             else "no JSON object")
                    raise JevError(f"there is no 'answers' object ({shape})", kind="protocol")
                answers = {}
                for key, question in chunk.items():
                    if key not in raw_answers:
                        raise JevError(f"the answer to question {key} is missing", kind="protocol")
                    answers[key] = _validate(key, question, raw_answers[key])
            except JevError as exc:
                raise JevError(f"{self.provider.label} answered, but not in the System One decision format: "
                               f"{exc}." + (" See docs/CUSTOM_MODELS.md for what IntentSQL expects."
                                            if self.provider.custom else ""), kind="protocol") from None
            reported = _mapping(data.get("usage"))
            model, request_id = data.get("model"), data.get("id")
            exchange = Exchange(purpose, state, body["questions"],
                                {k: raw_answers[k] for k in chunk},
                                int(_number(reported.get("input_tokens")) or 0),
                                int(_number(reported.get("output_tokens")) or 0),
                                round((time.monotonic() - t0) * 1000),
                                str(model)[:100] if isinstance(model, (str, int, float)) else None,
                                cached, self.provider.name, _number(reported.get("cost")),
                                str(request_id)[:100] if isinstance(request_id, (str, int)) else None)
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


def _detail(response: httpx.Response, key: str | None) -> str:
    """The provider's own short error message ('API key expired'), never echoing the key."""
    try:
        data = response.json()
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str) or not message.strip():
        return ""
    if key:
        message = message.replace(key, "***")
    return f" (provider says: {message.strip()[:160]})"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


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


class UnselectedClient:
    """Stands in when no model is selected: asking explains how to choose one."""

    configured = False
    model = ""

    def __init__(self) -> None:
        from intentsql.providers import Provider
        self.provider = Provider("", "No model selected", "", "", ("",), ())

    def close(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {"provider": "", "label": self.provider.label, "url": "", "model": "", "has_key": False,
                "budget_usd": None, "spent_usd": 0.0, "requests": 0}

    def ask(self, *args: Any, **kwargs: Any) -> dict[str, Answer]:
        raise JevError("No model is selected. Choose TypeSafe Jev, Liquid AI d1 or your own custom model "
                       "(the model button in the app, or `intent-sql keys typesafe|liquid` / "
                       "`intent-sql custom add`), or try demo mode (`intent-sql keys demo`).",
                       fatal=True, kind="not_configured")


def shared_client() -> Any:
    """The client for the selected provider; the demo client only when demo mode is chosen."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            from intentsql.config import settings
            name = settings().provider
            if name == "demo":
                from intentsql.mock import MockClient
                _SHARED = MockClient()
            elif not name:
                _SHARED = UnselectedClient()
            else:
                _SHARED = JevClient()
        return _SHARED


def reset_shared_client() -> None:
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is not None:
            _SHARED.close()
        _SHARED = None
