"""Regenerate benchmarks/suites/adversarial.json and validate every gold query by execution."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from adversarial_cases import suite  # noqa: E402
from build_retail import build  # noqa: E402


def main() -> None:
    db = build()
    data = suite()
    conn = sqlite3.connect(db)
    for case in data["cases"]:
        for sql in case["gold_sql"]:
            rows = conn.execute(sql).fetchall()
            if case["category"] != "misleading" and not rows:
                raise SystemExit(f"{case['id']}: gold returns no rows: {sql}")
    out = HERE.parent / "suites" / "adversarial.json"
    out.write_text(json.dumps(data, indent=1))
    print(f"{len(data['cases'])} cases written to {out}")


if __name__ == "__main__":
    main()
