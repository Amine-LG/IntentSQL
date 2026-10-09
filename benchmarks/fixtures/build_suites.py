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
from build_retail import build  # noqa: E402


def main() -> None:
    db = build()
    conn = sqlite3.connect(db)
    for module in (adversarial_cases, feature_cases):
        data = module.suite()
        for case in data["cases"]:
            results = [conn.execute(sql).fetchall() for sql in case["gold_sql"]]
            if case["category"] != "misleading" and not all(results):
                raise SystemExit(f"{case['id']}: a gold returns no rows")
        out = HERE.parent / "suites" / f"{data['suite']}.json"
        out.write_text(json.dumps(data, indent=1))
        print(f"{len(data['cases'])} cases written to {out}")


if __name__ == "__main__":
    main()
