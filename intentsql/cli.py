"""Command-line interface: ``intent-sql ask | schema | benchmark | serve``."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any


def _print_table(columns: list[str], rows: list[tuple[Any, ...]], limit: int = 30) -> None:
    if not columns:
        return
    shown = [[("NULL" if v is None else str(v)) for v in row] for row in rows[:limit]]
    widths = [min(40, max([len(c)] + [len(r[i]) for r in shown])) for i, c in enumerate(columns)]
    print("  ".join(c[:w].ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for row in shown:
        print("  ".join(v[:w].ljust(w) for v, w in zip(row, widths)))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more rows")


def cmd_ask(args: argparse.Namespace) -> int:
    from intentsql.pipeline import answer
    result = answer(args.database, args.question)
    if args.json:
        print(json.dumps(result.to_dict(debug=args.debug), indent=1, default=str))
        return 0 if result.status == "answered" else 1
    print(f"[{result.status}] {result.explanation or ''}")
    if result.message:
        print(result.message)
    if result.display_sql:
        print("\n" + result.display_sql + "\n")
    if result.status == "answered":
        _print_table(result.columns, result.rows)
    for alt in result.alternatives:
        print(f"  alternative: {alt.get('explanation')}")
    if args.debug:
        for d in result.decisions:
            print(f"  [{d.purpose}] {d.question} -> {d.selected} (p={d.probability:.2f})")
    u = result.usage
    print(f"\n{u.calls} Jev call(s), {u.input_tokens} input tokens, "
          f"{result.timings_ms.get('total', 0)} ms")
    return 0 if result.status == "answered" else 1


def cmd_schema(args: argparse.Namespace) -> int:
    from intentsql.schema import load_schema
    schema = load_schema(args.database)
    for table in schema.tables.values():
        cols = ", ".join(f"{c.name}:{c.kind}{'*' if c.primary_key else ''}" for c in table.columns)
        print(f"{table.name} ({table.row_count} rows): {cols}")
    for fk in schema.foreign_keys:
        print(f"  {fk.key}{'' if fk.declared else '  (inferred)'}")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    from intentsql.benchmark.runner import batch_statistics, run, summarize, write_report
    from intentsql.benchmark.spider import load_split, stratified_sample
    from intentsql.benchmark.systems import intentsql_system
    from intentsql.interpret import Strategy

    if args.split == "test" and not args.allow_test:
        print("The test split is held out. Pass --allow-test for a milestone evaluation; "
              "per-case details are redacted.", file=sys.stderr)
        return 2
    examples = load_split(args.split)
    if args.db:
        examples = tuple(e for e in examples if e.db_id in set(args.db))
    seeds = ([random.SystemRandom().randrange(1, 2**31) for _ in range(args.batches)]
             if args.random or args.seed is None else
             [args.seed + i for i in range(args.batches)])
    strategy = Strategy(**json.loads(args.strategy)) if args.strategy else None
    system = intentsql_system(strategy, keep_debug=args.split != "test")
    redact = args.split == "test"
    summaries = []
    all_cases = []
    for batch, seed in enumerate(seeds, start=1):
        sample = stratified_sample(examples, args.size, seed, by=args.by)
        started = time.monotonic()

        def progress(done: int, total: int, case) -> None:
            if not args.quiet:
                mark = "✓" if case.correct else ("·" if case.status != "answered" else "✗")
                print(mark, end="", flush=True)

        cases = run(system, sample, workers=args.workers, redact=redact,
                    keep_trace=not redact, progress=progress)
        summary = summarize(cases)
        summary["seed"] = seed
        summary["wall_s"] = round(time.monotonic() - started, 1)
        summaries.append(summary)
        all_cases.extend(cases)
        print(f"\nbatch {batch}/{len(seeds)} seed={seed}: "
              f"EX={summary['execution_accuracy']:.1%} answered={summary['answered_rate']:.1%} "
              f"acc|answered={(summary['accuracy_when_answered'] or 0):.1%} "
              f"calls={summary['jev_calls_mean']} tokens={summary['input_tokens_mean']} "
              f"p50={summary['latency_ms_p50']}ms")
    overall = summarize(all_cases)
    stats = batch_statistics(summaries) if len(summaries) > 1 else None
    printable = {k: v for k, v in overall.items() if not k.startswith("by_")}
    print(json.dumps(printable, indent=1))
    print(json.dumps({"by_hardness": overall["by_hardness"], "by_category": overall["by_category"]},
                     indent=1))
    if stats:
        print(json.dumps(stats, indent=1))
    if args.out:
        meta = {"split": args.split, "size": args.size, "batches": args.batches, "seeds": seeds,
                "by": args.by, "strategy": args.strategy, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                time.gmtime())}
        write_report(Path(args.out), meta, all_cases, overall,
                     {"batch_summaries": summaries, "batch_statistics": stats})
        print(f"report written to {args.out}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("intentsql.web:app", host=args.host, port=args.port, workers=1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intent-sql", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="answer one natural-language question")
    ask.add_argument("database")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true")
    ask.add_argument("--debug", action="store_true")
    ask.set_defaults(func=cmd_ask)

    schema = sub.add_parser("schema", help="show the grounded schema of a database")
    schema.add_argument("database")
    schema.set_defaults(func=cmd_schema)

    bench = sub.add_parser("benchmark", help="evaluate on Spider")
    bench.add_argument("--split", default="train", choices=("train", "dev", "test"))
    bench.add_argument("--size", type=int, default=50)
    bench.add_argument("--seed", type=int)
    bench.add_argument("--random", action="store_true", help="draw fresh random seeds")
    bench.add_argument("--batches", type=int, default=1)
    bench.add_argument("--by", default="hardness", choices=("hardness", "category"))
    bench.add_argument("--db", action="append", help="restrict to these database ids")
    bench.add_argument("--workers", type=int, default=6)
    bench.add_argument("--strategy", help="JSON overrides for interpretation strategy")
    bench.add_argument("--out")
    bench.add_argument("--quiet", action="store_true")
    bench.add_argument("--allow-test", action="store_true")
    bench.set_defaults(func=cmd_benchmark)

    serve = sub.add_parser("serve", help="start the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7862)
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
