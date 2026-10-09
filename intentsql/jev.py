"""Jev (TypeSafe System One) client: typed questions, validated answers, accounting.

Design rules:

* Every question is either a ``choice`` over options supplied by code or a
  ``noul`` (probability of yes).  Answers are validated against the options;
  an out-of-set answer is an error, never silently accepted.
* Large fan-outs are split into several requests that run concurrently, so a
  round of decisions costs one network round trip of latency.
* Usage (calls, tokens, latency, retries) is accounted per ``Session`` so a
  single request's cost is reported exactly, even when the client is shared.
* The API key is read from configuration and only ever placed in the HTTP
  ``Authorization`` header; it never appears in logs, traces or exchanges.
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

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# Keep each request comfortably inside Jev's 64k-token budget.
MAX_QUESTIONS_PER_REQUEST = 160
MAX_CHARS_PER_REQUEST = 90_000


class JevError(RuntimeError):
    """Transport or protocol failure talking to Jev."""


@dataclass(frozen=True)
class Question:
    kind: str                         # "choice" | "noul"
    instructions: Any
    options: Mapping[str, Any] | None = None   # choice: key -> description
    true: Any = None                  # noul criteria
    false: Any = None

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.kind, "instructions": self.instructions}
        if self.kind == "choice":
            body["criteria"] = dict(self.options or {})
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


@dataclass(frozen=True)
class Answer:
    kind: str
    choice: str | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    confidence: float | None = None
    p: float | None = None            # noul probability of "yes"

    @property
    def top_probability(self) -> float:
        if self.kind == "noul":
            return max(self.p or 0.0, 1 - (self.p or 0.0))
        return max(self.probabilities.values()) if self.probabilities else 0.0

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


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    rounds: int = 0
    latency_ms: int = 0
    cached_calls: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def _validate(key: str, question: Question, raw: Any) -> Answer:
    if not isinstance(raw, dict):
        raise JevError(f"Answer {key} is not an object")
    if question.kind == "noul":
        p = raw.get("noul")
        if not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise JevError(f"Answer {key} has no valid noul probability")
        return Answer("noul", p=float(p))
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


class JevClient:
    """Thread-safe HTTP client with keep-alive, retries and an optional cache."""

    def __init__(self, url: str | None = None, model: str | None = None,
                 api_key: str | None = None, *, timeout: float = 45.0,
                 max_retries: int = 3, cache_dir: str | Path | None = None,
                 max_parallel: int = 8) -> None:
        from intentsql.config import settings
        cfg = settings()
        self.url = url or cfg.jev_url
        self.model = model or cfg.jev_model
        self._api_key = api_key if api_key is not None else cfg.jev_api_key
        self.timeout = timeout
        self.max_retries = max_retries
        cache = cache_dir if cache_dir is not None else cfg.jev_cache_dir
        self.cache_dir = Path(cache) if cache else None
        self._http = httpx.Client(timeout=timeout, http2=False,
                                  limits=httpx.Limits(max_keepalive_connections=16))
        self._pool = ThreadPoolExecutor(max_workers=max_parallel)

    @property
    def configured(self) -> bool:
        return bool(self._api_key) or "127.0.0.1" in self.url or "localhost" in self.url

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

    def _post(self, body: dict[str, Any], usage: Usage, lock: threading.Lock) -> tuple[dict, bool]:
        cache = self._cache_path(body)
        if cache and cache.is_file():
            try:
                return json.loads(cache.read_text(encoding="utf-8")), True
            except (OSError, ValueError):
                pass
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "IntentSQL/1.0"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._http.post(self.url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                if attempt <= self.max_retries:
                    with lock:
                        usage.retries += 1
                    continue
                raise JevError("Jev did not respond in time.") from exc
            except httpx.HTTPError as exc:
                if attempt <= self.max_retries:
                    with lock:
                        usage.retries += 1
                    time.sleep(0.3 * attempt)
                    continue
                raise JevError("Could not reach Jev. Check the endpoint and network.") from exc
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise JevError("Jev returned invalid JSON.") from exc
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
            message = {401: "Jev rejected the API key. Check the connection settings.",
                       403: "Jev denied access for this key or model.",
                       429: "Jev rate limit reached; try again shortly."}.get(
                response.status_code, f"Jev returned HTTP {response.status_code}.")
            raise JevError(message)

    # -- public ------------------------------------------------------------------
    def ask(self, state: Any, questions: Mapping[str, Question], *, usage: Usage,
            log: list[Exchange] | None = None, purpose: str = "") -> dict[str, Answer]:
        """Ask all questions about ``state`` in as few concurrent requests as possible."""
        if not questions:
            return {}
        if not self.configured:
            raise JevError("No Jev API key configured. Set SYSTEM_ONE_API_KEY.")
        chunks = _chunk(questions)
        lock = threading.Lock()
        started = time.monotonic()

        def run(chunk: dict[str, Question]) -> tuple[dict[str, Answer], Exchange]:
            body: dict[str, Any] = {"state": state, "model": self.model,
                                    "questions": {k: q.payload() for k, q in chunk.items()}}
            t0 = time.monotonic()
            data, cached = self._post(body, usage, lock)
            raw_answers = data.get("answers") if isinstance(data, dict) else None
            if not isinstance(raw_answers, dict):
                raise JevError("Jev returned no answers object.")
            answers = {}
            for key, question in chunk.items():
                if key not in raw_answers:
                    raise JevError(f"Jev omitted answer {key}.")
                answers[key] = _validate(key, question, raw_answers[key])
            reported = data.get("usage") or {}
            exchange = Exchange(purpose, state, body["questions"],
                                {k: raw_answers[k] for k in chunk},
                                int(reported.get("input_tokens") or 0),
                                int(reported.get("output_tokens") or 0),
                                round((time.monotonic() - t0) * 1000),
                                data.get("model"), cached)
            return answers, exchange

        if len(chunks) == 1:
            results = [run(chunks[0])]
        else:
            results = list(self._pool.map(run, chunks))
        merged: dict[str, Answer] = {}
        with lock:
            usage.rounds += 1
            usage.latency_ms += round((time.monotonic() - started) * 1000)
            for answers, exchange in results:
                merged.update(answers)
                usage.calls += 1
                usage.cached_calls += exchange.cached
                usage.input_tokens += exchange.input_tokens
                usage.output_tokens += exchange.output_tokens
                if log is not None:
                    log.append(exchange)
        return merged


def _chunk(questions: Mapping[str, Question]) -> list[dict[str, Question]]:
    chunks: list[dict[str, Question]] = [{}]
    size = 0
    for key, question in questions.items():
        cost = len(json.dumps(question.payload(), ensure_ascii=False))
        if chunks[-1] and (len(chunks[-1]) >= MAX_QUESTIONS_PER_REQUEST
                           or size + cost > MAX_CHARS_PER_REQUEST):
            chunks.append({})
            size = 0
        chunks[-1][key] = question
        size += cost
    return chunks


_SHARED: JevClient | None = None
_SHARED_LOCK = threading.Lock()


def shared_client() -> JevClient:
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = JevClient()
        return _SHARED


def reset_shared_client() -> None:
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is not None:
            _SHARED.close()
        _SHARED = None
