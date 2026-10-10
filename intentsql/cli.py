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


def _database(name: str) -> Path:
    """A SQLite file path, or the name of a bundled or imported database ('dese.db')."""
    path = Path(name).expanduser()
    if path.is_file():
        return path
    from intentsql.workspace import WorkspaceError, resolve_database
    try:
        return resolve_database(name).path
    except WorkspaceError:
        raise SystemExit(f"No such database: {name}") from None


def cmd_ask(args: argparse.Namespace) -> int:
    from intentsql.pipeline import answer
    result = answer(_database(args.database), args.question)
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
    schema = load_schema(_database(args.database))
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

    if args.suite:
        return _run_suite(args)
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
                                                                                time.gmtime()),
                **_provenance()}
        write_report(Path(args.out), meta, all_cases, overall,
                     {"batch_summaries": summaries, "batch_statistics": stats})
        print(f"report written to {args.out}")
    return 0


def _provenance() -> dict[str, Any]:
    """Provider, model and code revision of a benchmark run (no secrets)."""
    import subprocess

    from intentsql.config import PROJECT_ROOT
    from intentsql.jev import shared_client
    info = shared_client().describe()
    try:
        revision = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                    cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        revision, dirty = "", False
    return {"provider": info["provider"], "provider_label": info["label"], "model": info["model"],
            "revision": revision + ("+dirty" if dirty else "")}


def _run_suite(args: argparse.Namespace) -> int:
    from intentsql.benchmark.suites import load_suite, run_suite, summarize_suite
    from intentsql.benchmark.systems import intentsql_system
    from intentsql.interpret import Strategy

    strategy = Strategy(**json.loads(args.strategy)) if args.strategy else None
    cases = load_suite(args.suite)
    results = run_suite(intentsql_system(strategy), cases, workers=args.workers)
    summary = summarize_suite(results)
    print(json.dumps(summary, indent=1))
    if args.show_failures:
        for r in results:
            if not r.correct:
                print(f"✗ {r.case.id} [{r.case.category}] {r.case.question}\n   {r.status}: "
                      f"{r.sql or r.message}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "suite": args.suite, "summary": summary, "meta": {**_provenance(), "time": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "results": [{"id": r.case.id, "category": r.case.category, "question": r.case.question,
                         "status": r.status, "correct": r.correct, "sql": r.sql, "message": r.message,
                         "confidence": r.confidence} for r in results]}, indent=1))
    return 0


def cmd_provider(args: argparse.Namespace) -> int:
    """Show the active provider; optionally check the account and send one tiny decision."""
    from intentsql.config import settings
    from intentsql.jev import JevError, Usage, choice, noul, score, shared_client
    cfg = settings()
    client = shared_client()
    info = client.describe()
    print(f"provider: {info['label']} [{info['provider']}] (requested: {cfg.provider_requested})")
    print(f"endpoint: {info['url'] or '-'}  model: {info['model']}  key: {'set' if info['has_key'] else 'missing'}")
    print(f"keys available for: {', '.join(cfg.keys_present) or 'none'}")
    if args.balance and info["provider"] == "openrouter" and info["has_key"]:
        print(_openrouter_balance(cfg.key_for("openrouter")))
    if args.ping:
        usage = Usage()
        try:
            answers = client.ask({"ticket": "I was charged twice for one subscription."}, {
                "team": choice("Which team should handle `ticket`?",
                               {"billing": "charges and refunds", "technical": "bugs"}),
                "urgency": score("How urgent is `ticket`?", ["routine", "soon", "blocked now"]),
                "refund": noul("Is `ticket` asking for money back?")}, usage=usage)
        except JevError as exc:
            print(f"ping failed: {exc}")
            return 1
        print(f"ping: choice={answers['team'].choice} score={answers['urgency'].score} "
              f"noul={answers['refund'].p:.2f} model={','.join(usage.models)} "
              f"tokens={usage.input_tokens}/{usage.output_tokens} cost=${usage.cost_usd:.6f}")
    return 0


def _openrouter_balance(key: str) -> str:
    """Account balance from OpenRouter's key endpoint (no model call, no cost)."""
    import httpx
    try:
        response = httpx.get("https://openrouter.ai/api/v1/key",
                             headers={"Authorization": f"Bearer {key}"}, timeout=15)
        data = response.json().get("data", {}) if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        data = {}
    if not data:
        return "balance: unavailable"
    fields = {k: data.get(k) for k in ("usage", "limit", "limit_remaining", "is_free_tier")}
    try:
        credits = httpx.get("https://openrouter.ai/api/v1/credits",
                            headers={"Authorization": f"Bearer {key}"}, timeout=15)
        totals = credits.json().get("data", {}) if credits.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        totals = {}
    if totals:
        fields["total_credits"] = totals.get("total_credits")
        fields["total_usage"] = totals.get("total_usage")
    return "balance: " + ", ".join(f"{k}={v}" for k, v in fields.items())


def cmd_keys(args: argparse.Namespace) -> int:
    """Store provider keys in ~/.config/intentsql/providers.env (0600), typed without echo."""
    import getpass
    import os

    from intentsql.config import _read_env_file, config_dir
    path = config_dir() / "providers.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    values = _read_env_file(path) if path.is_file() else {}
    for variable, label in (("OPENROUTER_API_KEY", "OpenRouter"), ("OPENJEV_API_KEY", "OpenJEV"),
                            ("SYSTEM_ONE_API_KEY", "TypeSafe direct")):
        entered = getpass.getpass(f"{label} API key (Enter to {'keep' if values.get(variable) else 'skip'}): ")
        if entered.strip():
            values[variable] = entered.strip()
    if args.provider:
        values["INTENTSQL_PROVIDER"] = args.provider
    fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{k}={v}\n" for k, v in values.items()))
    os.replace(path.with_suffix(".tmp"), path)
    os.chmod(path, 0o600)
    stored = [k for k in values if k.endswith("_API_KEY")]
    print(f"Saved {', '.join(stored) or 'no keys'} to {path} (owner-only). Keys are not shown.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("intentsql.web:app", host=args.host, port=args.port, workers=1)
    return 0


def _guarded(run_benchmark):
    """Report an evaluation stopped by a provider failure as interrupted, never as results."""
    def wrapper(args: argparse.Namespace) -> int:
        from intentsql.benchmark.systems import ProviderStopped
        try:
            return run_benchmark(args)
        except ProviderStopped as exc:
            print(f"\nINTERRUPTED: {exc}\nNo report was written; restart this evaluation once the "
                  "provider works (do not combine partial runs or providers).", file=sys.stderr)
            return 3
    return wrapper


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
    bench.add_argument("--suite", help="run a hand-written suite (benchmarks/suites/*.json) instead of Spider")
    bench.add_argument("--show-failures", action="store_true")
    bench.set_defaults(func=_guarded(cmd_benchmark))

    keys = sub.add_parser("keys", help="enter provider API keys (hidden input, stored owner-only)")
    keys.add_argument("--provider", choices=("auto", "openrouter", "typesafe", "openjev", "mock"))
    keys.set_defaults(func=cmd_keys)

    prov = sub.add_parser("provider", help="show the active Jev provider; check balance or ping it")
    prov.add_argument("--balance", action="store_true", help="OpenRouter: show credits (free call)")
    prov.add_argument("--ping", action="store_true", help="send one tiny choice/score/noul decision")
    prov.set_defaults(func=cmd_provider)

    serve = sub.add_parser("serve", help="start the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7862)
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
