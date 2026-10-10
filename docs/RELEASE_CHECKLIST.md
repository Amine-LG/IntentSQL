# Release checklist: 1.0.0-beta.1

Status as of 2026-10-10. ✅ done and verified locally · ⏳ needs a working decision-model key or credits · 👤 needs the owner's action.

## Software (verified offline)

| Item | Status | Evidence |
|---|---|---|
| Grounding, typed IR, compiler, read-only execution | ✅ | Offline test suite with a scripted decision-model double |
| Providers: TypeSafe Jev, Liquid AI d1, explicit demo mode; no hidden fallback | ✅ | tests/test_jev_client.py, tests/test_web.py |
| Model setup and connection states (not configured, ready to test, connected only after a real answer, key rejected, no credits, unavailable, incompatible, demo, locked by environment) | ✅ | Browser-tested in a clean environment; the Liquid key-rejected path was checked against the real endpoint with an invalid key |
| Keys server-side only (0600 file, never returned, never in `localStorage`) | ✅ | tests/test_web.py, tests/test_custom_models.py; the browser page and API responses were checked for the key after saving |
| Custom decision models: add, test, select, edit, remove; compatibility test per decision type; *Incompatible* state; HTTPS rule; key dropped on host change; never demo on failure or removal | ✅ | tests/test_custom_models.py (mock transport); scripts/smoke.sh (real HTTP, local protocol stub and chat-style API); browser-tested, desktop and mobile, light and dark |
| Decision inspector (found, decided, planned, checked, SQL and data); no probabilities shown in demo mode | ✅ | Browser-tested, desktop and 390 px mobile, light and dark |
| Evaluations stop on fatal provider errors | ✅ | tests/test_benchmark_eval.py (`INTERRUPTED`, no report) |
| CLI (`ask`, `schema`, `benchmark`, `serve`, `keys`, `provider`, `custom`, `--version`) | ✅ | scripts/smoke.sh |
| Packaging: wheel and sdist build without warnings; clean install in a fresh Python 3.12 venv; UI, example data and data notice included | ✅ | Local run of the CI `package` job steps |
| Docker: builds, runs as non-root, health check, demo answer | ✅ | Local run of the CI `docker` job steps |
| Secret scan, dependency audit, code scan | ✅ | docs/SECURITY.md |
| ruff, mypy, `node --check` | ✅ | Clean |
| GitHub Actions CI | ⏳ | Workflow written and its steps run locally; it has never run on GitHub (nothing is pushed) |

## Measurements

| Item | Status | Notes |
|---|---|---|
| Historical TypeSafe Jev results (Spider dev 76.8% ± 2.6) | ✅ | Code from 2026-10-09; docs/EVALUATION.md#results |
| TypeSafe Jev on this build | ⏳ | HTTP 402 (no credits) |
| Liquid AI d1: connection check, smoke, development suites | ⏳ | No key was available |
| Liquid AI d1 calibration table | ⏳ | Fit from its train runs (`scripts/fit_calibration.py`) before quoting its confident-answer accuracy |
| Frozen held-out suite; Spider test milestone | ⏳ | Run once, on frozen code |

## Before publishing (owner)

1. 👤 Review the final report, `git log main..v1` and the README.
2. ⏳ Optionally run a short live check:
   ```bash
   intent-sql keys liquid
   STEPS="check smoke" scripts/release_eval.sh liquid
   ```
3. 👤 Push `v1` and let GitHub Actions run; fix anything it finds.
4. 👤 Enable private vulnerability reporting (Settings > Code security), which docs/SECURITY.md refers to.
5. 👤 Merge into `main` and push the tag `v1.0.0-beta.1`. Create a GitHub *pre-release* from `docs/RELEASE_NOTES.md`.
6. 👤 Deactivate the development keys that were rejected (OpenRouter, OpenJEV), if they are not already.
