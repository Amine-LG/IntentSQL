# v1.0 release checklist

Status as of 2026-10-10. ✅ done and verified · ⏳ blocked (needs a working Jev provider) · ➖ not planned.

## Software (verified offline)

| Item | Status | Evidence |
|---|---|---|
| Grounding, typed IR, compiler, read-only execution | ✅ | 125 offline tests (scripted Jev double) |
| SQL feature coverage | ✅ | docs/CAPABILITIES.md; features suite at 92.9% (TypeSafe, 2026-10-10) |
| Provider abstraction (OpenRouter, TypeSafe direct, OpenJEV, mock), with no hidden fallback | ✅ | tests/test_jev_client.py (request format, cost, budget, selection, errors) |
| Credentials | ✅ | `~/.config/intentsql/providers.env` (0600) or the workspace settings (0600). Keys are sent only to their own provider and never returned, logged or committed. Tests check that a key never appears in exchanges or API responses |
| Evaluations stop on fatal provider errors | ✅ | tests/test_benchmark_eval.py; `INTERRUPTED`, no report written |
| Web UI | ✅ | Checked in Chrome: desktop and mobile, light and dark, settings and provider selector, ask flow, history, CSV, alternatives, mock labelling, no console errors |
| CLI | ✅ | `ask`, `schema`, `benchmark`, `serve`, `keys`, `provider` |
| Install | ✅ | `pip install .` in a fresh Python 3.12 venv; bundled databases and UI included |
| Docker | ✅ | Image builds; health, `/api/ask` (mock) and static files served |
| ruff, mypy | ✅ | clean (30 source files) |
| Documentation | ✅ | README, ARCHITECTURE, CAPABILITIES, EVALUATION, EXPERIMENTS, CHANGELOG |

## Measurements

| Item | Status | Notes |
|---|---|---|
| Spider dev with TypeSafe direct `jev-latest` | ✅ | 76.8% ± 2.6 EX; 86.0% accuracy on confident answers (2 × 300 random) |
| Regression of the latest commits | ⏳ | Elapsed time, cross-level arithmetic, flags, name pairs and the HAVING split are covered by offline tests but not yet re-measured live |
| Provider check (balance, ping, smoke suite) for OpenRouter | ⏳ | The key tried on 2026-10-10 was rejected: "API key expired" |
| OpenJEV | ⏳ | The key tried on 2026-10-10 was rejected: "Missing or invalid API key" |
| TypeSafe direct | ⏳ | HTTP 402, no credits |
| Development suites and train seeds 31/21 with the release provider | ⏳ | `scripts/release_eval.sh <provider>` |
| Calibration table for the release provider | ⏳ | Fit on that provider's train runs before quoting confident-answer accuracy (docs/EVALUATION.md) |
| Frozen held-out suite (45 cases, clinic schema) | ⏳ | Run once on frozen code |
| Spider test milestone (≥ 600 questions) | ⏳ | `--allow-test`; aggregates only |

## To finish the release (about $1 of provider credit)

```bash
.venv/bin/intent-sql keys --provider openrouter         # replacement key, hidden input
scripts/release_eval.sh openrouter                       # check, smoke, suites, train, dev, holdout, test
```

If OpenRouter cannot be used, run `scripts/release_eval.sh openjev` instead, and report its results separately. Afterwards:
1. Fit the provider's calibration table from its train runs.
2. Update the README results with the provider named.
3. Tag the release candidate locally: `git tag v1.0.0-rc1`. Nothing is pushed.
