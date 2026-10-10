"""Spending guard for paid decision-model requests.

Every request attempt (retries included) first *reserves* a conservative upper bound of
its cost; the request is refused if spent + reserved + this reservation would pass the
limit. When the attempt ends, the reservation is settled:

- an answer: the reported cost, or the reported input tokens at the listed price plus a
  margin (output tokens are free for System One providers);
- a timeout, connection failure or server error (5xx): the full reservation, because the
  provider may have processed and billed the request;
- a rejected request (4xx): nothing.

With ``INTENTSQL_SPEND_LEDGER=<file>`` the totals persist in that file, shared by every
process (file-locked on POSIX), so a limit holds across runs and is never reset by
starting a new one. The ledger keeps the lowest limit it has ever been given; a higher
``INTENTSQL_BUDGET_USD`` cannot raise it. Reservations left by a process that died
mid-request are never released, which keeps the total an upper bound.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MARGIN = 1.10              # on top of reported tokens (tokenisation and billing differences)
CHARS_PER_TOKEN = 2.0      # deliberately low: typical JSON is about 3.5-4 characters per token


def reservation(body_chars: int, price_per_mtok: float | None) -> float:
    """A conservative upper bound of a request's cost, before it is sent."""
    if not price_per_mtok:
        return 0.0
    return body_chars / CHARS_PER_TOKEN * price_per_mtok / 1_000_000 * MARGIN


def billed(input_tokens: float | None, reported_cost: float | None, price_per_mtok: float | None,
           reserved: float) -> float:
    """What an answered request is counted as."""
    if reported_cost is not None:
        return reported_cost
    if input_tokens and price_per_mtok:
        return input_tokens * price_per_mtok / 1_000_000 * MARGIN
    return reserved                      # no usage reported: keep the upper bound


class LimitReached(Exception):
    pass


class Budget:
    """Spent and reserved totals against an optional limit; optionally persisted."""

    def __init__(self, limit: float | None, ledger: str | Path | None = None) -> None:
        self.limit = limit
        self.ledger = Path(ledger).expanduser() if ledger else None
        self._lock = threading.Lock()
        self._state = {"spent_usd": 0.0, "reserved_usd": 0.0, "requests": 0, "input_tokens": 0}
        if self.ledger:
            with self._locked() as state:
                stored = state.get("limit_usd")
                if isinstance(stored, (int, float)):
                    self.limit = stored if limit is None else min(stored, limit)
                state["limit_usd"] = self.limit

    # -- storage ---------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            if not self.ledger:
                yield self._state
                return
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(self.ledger.with_suffix(".lock"), "a+") as lock_file:
                try:
                    import fcntl
                    fcntl.flock(lock_file, fcntl.LOCK_EX)
                except ImportError:                    # Windows: thread lock only
                    pass
                try:
                    state = json.loads(self.ledger.read_text(encoding="utf-8"))
                    if not isinstance(state, dict):
                        raise ValueError
                except (OSError, ValueError):
                    state = {}
                for key, value in self._state.items():
                    state.setdefault(key, value if key != "spent_usd" else 0.0)
                yield state
                state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                tmp = self.ledger.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
                os.replace(tmp, self.ledger)

    # -- accounting -------------------------------------------------------------------
    def reserve(self, amount: float) -> float:
        with self._locked() as state:
            committed = state["spent_usd"] + state["reserved_usd"]
            if self.limit is not None and committed + amount > self.limit:
                raise LimitReached(f"The spending limit of ${self.limit:.2f} would be passed "
                                   f"(${state['spent_usd']:.4f} spent, ${state['reserved_usd']:.4f} in "
                                   "flight or unsettled); no further requests are sent.")
            state["reserved_usd"] += amount
        return amount

    def settle(self, reserved: float, cost: float, input_tokens: float | None = None) -> None:
        with self._locked() as state:
            state["reserved_usd"] = max(0.0, state["reserved_usd"] - reserved)
            state["spent_usd"] += cost
            state["requests"] += 1
            state["input_tokens"] += int(input_tokens or 0)

    def snapshot(self) -> dict[str, Any]:
        with self._locked() as state:
            return {**state, "limit_usd": self.limit}

    @property
    def spent(self) -> float:
        return float(self.snapshot()["spent_usd"])

    @property
    def requests(self) -> int:
        return int(self.snapshot()["requests"])
