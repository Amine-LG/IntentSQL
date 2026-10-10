#!/usr/bin/env bash
# Release evaluation, in protocol order (docs/EVALUATION.md), for ONE provider.
#
#   scripts/release_eval.sh openrouter            # or openjev / typesafe
#   STEPS="check smoke" scripts/release_eval.sh openrouter
#
# Every report goes to ../IntentSQL-artifacts/v1-runs/<provider>/ and records the provider,
# model and code revision.  Nothing falls back to another provider: if the provider fails,
# the script stops (set -e) and a later run must restart the interrupted step.
# Cost guard: INTENTSQL_BUDGET_USD (default 3) stops requests once reported spend reaches it.
set -euo pipefail

provider="${1:?usage: release_eval.sh <openrouter|openjev|typesafe>}"
steps="${STEPS:-check smoke dev-suites train dev holdout test}"
root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/../IntentSQL-artifacts/v1-runs/$provider"
mkdir -p "$out"
export INTENTSQL_PROVIDER="$provider" INTENTSQL_BUDGET_USD="${INTENTSQL_BUDGET_USD:-3}"
run() { "$root/.venv/bin/intent-sql" "$@"; }
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

# About 10k input tokens and 2 calls per question; Jev lists $0.042 per million input tokens,
# so roughly $0.0005 per question.  The full plan below is about 2,000 questions (~$1).
echo "provider=$provider budget=\$$INTENTSQL_BUDGET_USD steps=$steps"

for step in $steps; do
  case "$step" in
    check)      run provider --balance --ping ;;
    smoke)      run benchmark --suite "$root/benchmarks/suites/smoke.json" --show-failures --out "$out/smoke-$stamp.json" ;;
    dev-suites) run benchmark --suite "$root/benchmarks/suites/features.json" --show-failures --out "$out/features-$stamp.json"
                run benchmark --suite "$root/benchmarks/suites/adversarial.json" --show-failures --out "$out/adversarial-$stamp.json" ;;
    train)      for seed in 31 21; do
                  run benchmark --split train --size 300 --seed "$seed" --workers 8 --quiet --out "$out/train-300-s$seed-$stamp.json"
                done ;;
    dev)        run benchmark --split dev --size 300 --random --batches 2 --workers 8 --quiet --out "$out/dev-random2x300-$stamp.json" ;;
    holdout)    # Run once, on frozen code; do not tune against it afterwards.
                run benchmark --suite "$root/benchmarks/suites/holdout.json" --show-failures --out "$out/holdout-$stamp.json" ;;
    test)       run benchmark --split test --size 300 --random --batches 2 --workers 8 --quiet --allow-test --out "$out/test-random2x300-$stamp.json" ;;
    *) echo "unknown step $step" >&2; exit 2 ;;
  esac
done
