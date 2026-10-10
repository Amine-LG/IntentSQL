"""Check that IntentSQL IR + compiler reproduce Spider reference results.

Converts every parsed reference query to IR, compiles it, executes both and
compares results.  Prints coverage and the first few mismatches.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intentsql.benchmark.exec_match import execute, order_matters, results_equivalent
from intentsql.benchmark.spider import SPLIT_FILES, spider_root
from intentsql.benchmark.spider_ir import Unconvertible, to_ir
from intentsql.compiler import CompileError, compile_query
from intentsql.schema import load_schema


def main(split: str, limit: int | None = None, show: int = 8) -> None:
    root = spider_root()
    data = json.loads((root / SPLIT_FILES[split][0]).read_text(encoding="utf-8"))[:limit]
    outcome: Counter[str] = Counter()
    shown = 0
    for index, item in enumerate(data):
        db = root / SPLIT_FILES[split][1] / item["db_id"] / f"{item['db_id']}.sqlite"
        try:
            query = to_ir(split, item["db_id"], item["sql"])
        except Unconvertible as exc:
            outcome[f"unconvertible:{exc}"] += 1
            continue
        try:
            compiled = compile_query(query, load_schema(db))
        except CompileError as exc:
            outcome["compile_error"] += 1
            if shown < show:
                shown += 1
                print("COMPILE", index, item["query"], exc, sep="\n  ")
            continue
        gold = execute(db, item["query"])
        mine = execute(db, compiled.sql, compiled.params)
        if gold.error:
            outcome["gold_error"] += 1
        elif mine.error:
            outcome["sql_error"] += 1
            if shown < show:
                shown += 1
                print("SQLERR", index, item["query"], compiled.display_sql, mine.error, sep="\n  ")
        elif results_equivalent(gold.rows, mine.rows, order_matters=order_matters(item["query"])):
            outcome["match"] += 1
        else:
            outcome["mismatch"] += 1
            if shown < show:
                shown += 1
                print("MISMATCH", index, item["query"], compiled.display_sql, sep="\n  ")
    total = len(data)
    print(split, total, {k: v for k, v in outcome.most_common()})
    print(f"match rate: {outcome['match'] / total:.1%}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
