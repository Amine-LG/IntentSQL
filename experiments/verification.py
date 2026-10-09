"""E7: can a plan-level check predict wrong answers (for abstention)?

For each case: run the normal interpretation, score correctness against the
reference, and record candidate confidence signals:

  * min_margin   -- smallest top-1/top-2 margin among recorded Choice decisions
  * verify       -- Noul: does the English interpretation say exactly what was asked?
  * verify_rows  -- the same, with the row count and first rows as extra evidence

Reports AUC per signal and accuracy when answering only the most confident
fraction of requests.  Development split only.

    python experiments/verification.py train 300 31
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intentsql.benchmark.exec_match import execute, order_matters, results_equivalent
from intentsql.benchmark.spider import load_split, stratified_sample
from intentsql.jev import Usage, noul, shared_client
from intentsql.pipeline import answer

VERIFY = ("Does `interpretation` describe exactly what `request` asks for, with nothing "
          "missing, nothing extra and the same conditions?")


def run_case(example):
    result = answer(example.db_path, example.question)
    if result.status != "answered" or not result.sql:
        return None
    gold = execute(example.db_path, example.query)
    mine = execute(example.db_path, result.sql, result.params)
    correct = bool(gold.rows is not None and mine.rows is not None and results_equivalent(
        gold.rows, mine.rows, order_matters=order_matters(example.query)))
    margins = [d.probability for d in result.decisions if d.probabilities]
    preview = {"row_count": len(result.rows), "first_rows": [list(r) for r in result.rows[:3]]}
    questions = {
        "verify": noul(VERIFY),
        "verify_rows": noul({"result": "`result` shows what the interpretation returned.",
                             "question": VERIFY}),
    }
    usage = Usage()
    state = {"request": example.question, "interpretation": result.explanation}
    a = shared_client().ask(state, {"verify": questions["verify"]}, usage=usage)
    b = shared_client().ask({**state, "result": preview}, {"verify_rows": questions["verify_rows"]},
                            usage=usage)
    return {"correct": correct, "min_margin": min(margins) if margins else 1.0,
            "verify": a["verify"].p, "verify_rows": b["verify_rows"].p,
            "key": example.key, "explanation": result.explanation}


def auc(points):
    pos = [s for s, c in points if c]
    neg = [s for s, c in points if not c]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main(split: str, size: int, seed: int) -> None:
    sample = stratified_sample(load_split(split), size, seed)
    with ThreadPoolExecutor(8) as pool:
        rows = [r for r in pool.map(run_case, sample) if r]
    base = sum(r["correct"] for r in rows) / len(rows)
    print(f"answered {len(rows)}  accuracy {base:.1%}")
    for signal in ("min_margin", "verify", "verify_rows"):
        points = [(r[signal], r["correct"]) for r in rows]
        ranked = sorted(points, key=lambda p: -p[0])
        curve = []
        for coverage in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
            kept = ranked[:max(1, round(coverage * len(ranked)))]
            curve.append(f"{coverage:.0%}:{sum(c for _, c in kept) / len(kept):.1%}")
        print(f"{signal:12} AUC={auc(points):.3f}  " + "  ".join(curve))
    Path("../IntentSQL-artifacts/v1-runs").mkdir(parents=True, exist_ok=True)
    Path(f"../IntentSQL-artifacts/v1-runs/exp-verify-{split}-{size}-s{seed}.json").write_text(
        json.dumps(rows, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
