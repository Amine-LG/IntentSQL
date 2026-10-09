"""Adapters exposing NL->SQL systems to the benchmark runner."""

from __future__ import annotations

from pathlib import Path

from intentsql.benchmark.runner import AMBIGUOUS, ANSWERED, ERROR, REFUSED, Prediction
from intentsql.interpret import Strategy

_STATUS = {"answered": ANSWERED, "ambiguous": AMBIGUOUS, "refused": REFUSED, "error": ERROR}


def intentsql_system(strategy: Strategy | None = None, *, keep_debug: bool = False):
    from intentsql.pipeline import answer

    def system(db_path: Path, question: str) -> Prediction:
        result = answer(db_path, question, strategy=strategy, execute=False)
        trace = None
        if keep_debug:
            trace = {"explanation": result.explanation, "intent": result.intent,
                     "decisions": [d.__dict__ for d in result.decisions],
                     "alternatives": result.alternatives}
        return Prediction(_STATUS[result.status], result.sql, result.params, result.message,
                          result.usage.calls, result.usage.input_tokens,
                          result.usage.output_tokens, result.usage.retries,
                          result.timings_ms.get("total", 0), trace)
    return system
