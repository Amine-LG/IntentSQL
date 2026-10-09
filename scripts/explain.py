"""Show the Jev decisions behind one request: python scripts/explain.py DB "question"."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intentsql.pipeline import answer


def main(db: str, question: str) -> None:
    result = answer(db, question)
    print(result.status, result.display_sql or result.message)
    print(result.explanation)
    for d in sorted(result.decisions, key=lambda d: (d.purpose, -d.probability)):
        top = ""
        if d.probabilities:
            ranked = sorted(d.probabilities.items(), key=lambda kv: -kv[1])[:3]
            top = "  " + ", ".join(f"{(d.options or {}).get(k, k)[:40]}={p:.2f}" for k, p in ranked)
        print(f"  [{d.purpose}] {str(d.question)[:70]} -> {d.selected} p={d.probability:.2f}{top}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
