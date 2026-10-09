"""One-off baseline: the published Alpha read path on random Spider cases.

Kept for the audit record (docs/AUDIT.md). Removed runtime code can be checked
out from the initial commit to reproduce it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intentsql.benchmark.runner import (ANSWERED, ERROR, REFUSED, Prediction, run, summarize,
                                        write_report)
from intentsql.benchmark.spider import load_split, stratified_sample


def load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def alpha(db_path: Path, question: str) -> Prediction:
    from intentsql.jev_client import JevClient
    from intentsql.semantic_read import run_read
    client = JevClient()
    started = time.monotonic()
    try:
        result = run_read(db_path, question, client)
        status, sql, params, message = ANSWERED, result["sql"], tuple(result["params"]), None
    except ValueError as exc:
        status, sql, params, message = REFUSED, None, (), str(exc)
    except Exception as exc:  # noqa: BLE001 - recorded as system error
        status, sql, params, message = ERROR, None, (), f"{type(exc).__name__}: {exc}"
    stats = client.usage_stats()
    return Prediction(status, sql, params, message, stats["jev_calls"], stats["input_tokens"],
                      stats["output_tokens"], 0, round((time.monotonic() - started) * 1000))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="train")
    parser.add_argument("--size", type=int, default=60)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    load_env()
    sample = stratified_sample(load_split(args.split), args.size, args.seed)
    cases = run(alpha, sample, workers=6)
    summary = summarize(cases)
    write_report(Path(args.out), {"system": "alpha-0.1.0", "split": args.split,
                                  "size": args.size, "seed": args.seed}, cases, summary)
    print(json.dumps({k: v for k, v in summary.items() if not k.startswith("by_")}, indent=1))
    print(json.dumps(summary["by_hardness"], indent=1))
