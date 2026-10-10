"""Regenerate benchmarks/suites/adversarial.json and validate every gold query by execution."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import adversarial_cases  # noqa: E402
import feature_cases  # noqa: E402
import holdout_cases  # noqa: E402
from build_clinic import build as build_clinic  # noqa: E402
from build_retail import build as build_retail  # noqa: E402

SMOKE = ("f-proj-1", "f-cmp-1", "f-bool-1", "f-like-1", "f-null-1", "f-limit-1", "f-join-1", "f-self-2",
         "f-agg-1", "f-having-1", "f-anti-2", "f-expr-2", "f-nest-2", "f-month-1", "f-range-1")


def main() -> None:
    retail, clinic = build_retail(), build_clinic()
    for module, db in ((adversarial_cases, retail), (feature_cases, retail), (holdout_cases, clinic)):
        conn = sqlite3.connect(db)
        data = module.suite()
        for case in data["cases"]:
            results = [conn.execute(sql).fetchall() for sql in case["gold_sql"]]
            if case["category"] != "misleading" and not all(results):
                raise SystemExit(f"{case['id']}: a gold returns no rows")
        out = HERE.parent / "suites" / f"{data['suite']}.json"
        out.write_text(json.dumps(data, indent=1))
        print(f"{len(data['cases'])} cases written to {out}")
        if module is feature_cases:
            smoke = {**data, "suite": "smoke",
                     "description": "15 feature cases for checking a provider end to end (cheap).",
                     "cases": [c for c in data["cases"] if c["id"] in SMOKE]}
            (HERE.parent / "suites" / "smoke.json").write_text(json.dumps(smoke, indent=1))
            print(f"{len(smoke['cases'])} cases written to smoke.json")


if __name__ == "__main__":
    main()
