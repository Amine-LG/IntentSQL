"""E10: which verification question best separates correct from wrong readings?

Re-scores the labelled interpretations recorded by experiments/verification.py
(request + English interpretation + correctness) with alternative questions.

    python experiments/verify_prompts.py ../IntentSQL-artifacts/v1-runs/exp-verify-train-300-s31.json
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intentsql.benchmark.spider import load_split
from intentsql.jev import Question, Usage, noul, shared_client

VARIANTS: dict[str, dict[str, Question]] = {
    "v0_exact": {"v": noul("Does `interpretation` describe exactly what `request` asks for, with nothing "
                           "missing, nothing extra and the same conditions?")},
    "v1_same_meaning": {"v": noul("Would answering `interpretation` give the person who wrote `request` "
                                  "the information they asked for? Ignore wording differences.")},
    "v2_missing_extra": {
        "missing": noul("Does `request` ask for a value, condition, grouping or ordering that "
                        "`interpretation` leaves out?"),
        "extra": noul("Does `interpretation` add a value, condition or restriction that `request` "
                      "does not ask for?")},
    "v3_score": {"v": Question("score", "How well does `interpretation` capture what `request` asks for? "
                                        "Ignore wording; judge meaning.",
                               None)},
}
SCORE_LEVELS = ["it answers a different question",
                "it misses or changes an important part of the request",
                "it matches the request except for a minor detail",
                "it asks for exactly the same information"]


def payload_fix(q: Question) -> Question:
    return q


def score_question(key: str, q: Question) -> dict:
    if q.kind == "score":
        return {"type": "score", "instructions": q.instructions, "criteria": SCORE_LEVELS}
    return q.payload()


def ask_raw(state, questions):
    client = shared_client()
    body = {"state": state, "model": client.model,
            "questions": {k: score_question(k, q) for k, q in questions.items()}}
    import threading
    data, _ = client._post(body, Usage(), threading.Lock())
    return data["answers"]


def auc(points):
    pos = [s for s, c in points if c]
    neg = [s for s, c in points if not c]
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main(path: str) -> None:
    rows = json.loads(Path(path).read_text())
    questions = {e.key: e.question for e in load_split("train")}

    def run(row):
        state = {"request": questions[row["key"]], "interpretation": row["explanation"]}
        out = {}
        for name, qs in VARIANTS.items():
            answers = ask_raw(state, qs)
            if name == "v2_missing_extra":
                out[name] = (1 - answers["missing"]["noul"]) * (1 - answers["extra"]["noul"])
            elif name == "v3_score":
                out[name] = answers["v"]["score"] / (len(SCORE_LEVELS) - 1)
            else:
                out[name] = answers["v"]["noul"]
        return out

    with ThreadPoolExecutor(8) as pool:
        scores = list(pool.map(run, rows))
    for name in VARIANTS:
        points = [(s[name], r["correct"]) for s, r in zip(scores, rows)]
        ranked = sorted(points, key=lambda p: -p[0])
        curve = "  ".join(f"{c:.0%}:{sum(x for _, x in ranked[:round(c * len(ranked))]) / round(c * len(ranked)):.1%}"
                          for c in (0.9, 0.8, 0.7))
        print(f"{name:18} AUC={auc(points):.3f}  {curve}")


if __name__ == "__main__":
    main(sys.argv[1])
