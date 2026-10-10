"""Command-line interface: ``intent-sql ask | schema | benchmark | serve``."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from intentsql import __version__


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
    demo = result.provider == "demo"
    print(f"[{result.status}{' · demo' if demo else ''}] {result.explanation or ''}".rstrip())
    if demo:
        print("Demo mode: fixed default choices, not a reading of the question.")
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
    source = "demo" if demo else (result.provider or "model")
    print(f"\n{u.calls} {source} call(s), {u.input_tokens} input tokens, "
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
    """Show the selected model provider; optionally send one tiny decision to test it."""
    from intentsql.config import settings
    from intentsql.jev import TEST_STATE, JevError, Usage, connection_test_questions, shared_client
    cfg = settings()
    client = shared_client()
    info = client.describe()
    if cfg.notice:
        print(cfg.notice)
    print(f"provider: {info['label']} [{info['provider'] or 'none'}]"
          f"{' (set by INTENTSQL_PROVIDER)' if cfg.locked_by_environment else ''}")
    print(f"model: {info['model'] or '-'}  key: {'not needed' if info['provider'] == 'demo' else 'set' if info['has_key'] else 'none (optional)' if client.provider.custom else 'missing'}  "
          f"keys saved for: {', '.join(k for k in cfg.keys_present if k != 'demo') or 'none'}")
    if args.test:
        usage = Usage()
        try:
            answers = client.ask(TEST_STATE, connection_test_questions(client.provider.kinds), usage=usage)
        except JevError as exc:
            print(f"test failed: {exc}")
            return 1
        parts = [f"choice={answers['team'].choice}" if "team" in answers else "",
                 f"noul={answers['refund'].p:.2f}" if "refund" in answers else "",
                 f"score={answers['urgency'].score}" if "urgency" in answers else ""]
        print(("demo check (no model contacted): " if info["provider"] == "demo" else "test passed: ")
              + " ".join(p for p in parts if p)
              + f" model={','.join(usage.models) or info['model']} input tokens={usage.input_tokens}")
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    """Save a provider's API key in the IntentSQL workspace (owner-only), typed without echo."""
    import getpass

    from intentsql.config import all_providers
    from intentsql.providers import ALIASES
    from intentsql.workspace import WorkspaceError, save_connection
    providers = all_providers()
    info = providers.get(ALIASES.get(args.provider, args.provider))
    if info is None:
        print(f"Unknown provider {args.provider}; use one of: {', '.join(providers)}")
        return 2
    hint = "leave empty to keep the saved key" + (", or if it needs none" if info.custom else "")
    key = "" if info.offline else getpass.getpass(
        f"{info.label} {info.model_label} API key ({info.key_help or hint}): ").strip()
    try:
        save_connection(provider=args.provider, api_key=key or None)
    except WorkspaceError as exc:
        print(exc)
        return 1
    name = info.label if info.offline else f"{info.label} {info.model_label}"
    print(f"{name} selected" + (" and its key saved (not shown)." if key else "."))
    if not info.offline:
        print("Run 'intent-sql provider --test' to check the connection.")
    return 0


def cmd_custom(args: argparse.Namespace) -> int:
    """Add, list or remove custom decision models (System One protocol endpoints)."""
    import getpass

    from intentsql.config import all_providers, settings
    from intentsql.providers import CUSTOM_PREFIX, missing_kinds
    from intentsql.workspace import WorkspaceError, remove_custom_model, save_custom_model
    try:
        if args.action == "list":
            customs = [p for p in all_providers().values() if p.custom]
            if not customs:
                print("No custom models. Add one with: intent-sql custom add NAME --url URL --model ID")
            for p in customs:
                has_key = "key saved" if p.name in settings().keys_present else "no key"
                missing = missing_kinds(p)
                print(f"{p.name}  {p.label}  {p.url}  model={p.model}  types={','.join(p.kinds)}  {has_key}"
                      + (f"  (cannot be used: needs {', '.join(missing)})" if missing else "")
                      + ("  [selected]" if p.name == settings().provider else ""))
            return 0
        if args.action == "remove":
            remove_custom_model(args.name.removeprefix(CUSTOM_PREFIX))
            print(f"Removed {args.name} and its key.")
            return 0
        key = getpass.getpass("API key (hidden; leave empty if the endpoint needs none): ").strip() \
            if args.key else None
        name = save_custom_model(slug=args.edit.removeprefix(CUSTOM_PREFIX) if args.edit else None,
                                 label=args.name, url=args.url, model=args.model,
                                 kinds=args.types.split(","), max_chars=args.max_chars,
                                 api_key=key or None, select=args.use)
    except WorkspaceError as exc:
        print(exc)
        return 1
    print(f"Saved {name}" + (" and selected it" if args.use else "")
          + ". It is not verified by IntentSQL: run 'intent-sql provider --test' after selecting it.")
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
    parser.add_argument("--version", action="version", version=f"IntentSQL {__version__} (beta)")
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

    keys = sub.add_parser("keys", help="choose a model provider and save its API key (hidden input)")
    keys.add_argument("provider", help="typesafe, liquid, demo, or custom:<id>")
    keys.set_defaults(func=cmd_keys)

    prov = sub.add_parser("provider", help="show the selected model provider; --test sends one tiny decision")
    prov.add_argument("--test", action="store_true", help="send one tiny question per decision type")
    prov.set_defaults(func=cmd_provider)

    custom = sub.add_parser("custom", help="add, list or remove custom decision models (your own endpoint)")
    custom_sub = custom.add_subparsers(dest="action", required=True)
    add = custom_sub.add_parser("add", help="add (or with --edit, change) a System One-compatible endpoint")
    add.add_argument("name", help="a display name, e.g. 'My lab model'")
    add.add_argument("--url", required=True, help="the endpoint URL (https://, or http:// on localhost)")
    add.add_argument("--model", required=True, help="the model ID the endpoint expects")
    add.add_argument("--types", default="choice,noul",
                     help="decision types it supports: choice, noul, score (default: choice,noul)")
    add.add_argument("--max-chars", type=int, default=None, help="largest request, in characters")
    add.add_argument("--key", action="store_true", help="prompt for an API key (hidden input)")
    add.add_argument("--use", action="store_true", help="select it after saving")
    add.add_argument("--edit", metavar="ID", default=None, help="change the existing custom model ID")
    custom_sub.add_parser("list", help="list custom models")
    remove = custom_sub.add_parser("remove", help="remove a custom model and its key")
    remove.add_argument("name", help="its ID, e.g. custom:my-lab-model")
    custom.set_defaults(func=cmd_custom)

    serve = sub.add_parser("serve", help="start the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7862)
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
