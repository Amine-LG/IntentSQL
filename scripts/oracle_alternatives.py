"""How often is a one-flip alternative correct when the main interpretation is wrong?"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from concurrent.futures import ThreadPoolExecutor

from intentsql.benchmark.exec_match import execute, order_matters, results_equivalent
from intentsql.benchmark.spider import load_split, stratified_sample
from intentsql.compiler import compile_query
from intentsql.grounding import ground
from intentsql.intent import build_query
from intentsql.interpret import alternatives, interpret
from intentsql.jev import Usage, shared_client
from intentsql.schema import load_schema


def correct(example, intent, schema):
    try:
        compiled = compile_query(build_query(schema, intent), schema)
    except Exception:
        return False
    gold = execute(example.db_path, example.query)
    mine = execute(example.db_path, compiled.sql, compiled.params)
    return bool(gold.rows is not None and mine.rows is not None and results_equivalent(
        gold.rows, mine.rows, order_matters=order_matters(example.query)))


def one(example):
    schema = load_schema(example.db_path)
    g = ground(example.question, schema)
    usage = Usage()
    client = shared_client()
    interp, cands = interpret(g, lambda s, q, p: client.ask(s, q, usage=usage))
    if interp.intent is None:
        return (False, False, 0)
    base = correct(example, interp.intent, schema)
    alts = alternatives(g, cands, interp)
    any_alt = any(correct(example, a, schema) for _, a in alts)
    return (base, base or any_alt, len(alts))


if __name__ == "__main__":
    sample = stratified_sample(load_split(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(one, sample))
    n = len(results)
    print(f"base {sum(r[0] for r in results)/n:.1%}  oracle@1flip {sum(r[1] for r in results)/n:.1%}"
          f"  mean alternatives {sum(r[2] for r in results)/n:.2f}")
