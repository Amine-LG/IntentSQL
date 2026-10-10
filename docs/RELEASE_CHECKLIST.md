# Release checklist: 1.0.0

Status as of 2026-10-10. ✅ verified locally · ⏳ needs GitHub (nothing is pushed) · 👤 owner's action.

A stable 1.0 does not answer every question. It must reliably deliver a documented scope (docs/CAPABILITIES.md), fail safely and visibly outside it, and say honestly how often it is wrong.

## Stable release criteria

| Criterion | Status | Evidence |
|---|---|---|
| Installs cleanly | ✅ | Wheel and sdist build without warnings; fresh Python 3.12 venv install; `scripts/smoke.sh` from outside the source tree |
| Real-model integration works | ✅ | Live TypeSafe `jev-1.13.0`: authentication; Choice, Noul and Score answers parsed; live smoke suite 15/15 (one case per SQL category) |
| Model selection works; no silent demo fallback | ✅ | tests/test_web.py, tests/test_custom_models.py; browser: setup → Test connection → *Connected*; no key → visible error, never demo |
| Supported SQL behaves correctly | ✅ | Capability suite 94.6% (live); offline scenario tests (152 tests in total) |
| Read-only execution | ✅ | Read-only connections, row and time limits, writes refused (tests); console rejects writes |
| Errors and recovery | ✅ | Fatal provider errors stop evaluations without results (`INTERRUPTED`); the UI shows the failed step and the provider's message |
| No regressions | ✅ | Identical historical questions: Spider train 76.8% (historically 75.8%), Spider dev 76.7% (historically 76.8%); one development regression fixed with a test |
| Generalization to unfamiliar schemas | ✅ | Held-out clinic suite, run once on frozen code: 93.3% (42/45, interval 82–98%); Spider test sample, 600 questions on test databases: 78.0% |
| Honest uncertainty | ✅ | Calibration checked on 600 fresh live answers (each bin within 3 points); uncertain answers flagged with alternatives; per-decision probabilities shown apart from confidence; demo mode shows none |
| No high-severity security issues | ✅ | docs/SECURITY.md: secret scans, key-value scan of all Git objects and artifacts, dependency audit, code scan, earlier independent review fixed |
| Accurate documentation | ✅ | README, INSTALL, ARCHITECTURE, CAPABILITIES, EVALUATION (every run, with seeds), CUSTOM_MODELS, SECURITY, CHANGELOG, RELEASE_NOTES |
| UI and CLI work | ✅ | Browser: desktop and 390 px mobile, dark and light, first run, model setup, a live answer, decision inspector, alternatives, error state, keyboard; no console errors. CLI: `ask`, `schema`, `benchmark`, `serve`, `keys`, `provider`, `custom`, `--version` |
| Reproducible build and artifacts | ✅ | Pinned requirements; Docker image builds and runs as non-root; package includes the UI, example data and its licence notice |
| GitHub Actions CI | ⏳ | Workflow written; each job's steps run locally. It has never run on GitHub; Windows untested |

## Known limitations (documented, not blockers)

- Accuracy is 71–78% on Spider-style questions; between one in eight and one in five confident answers is wrong; extra-hard questions score 62–65%.
- Only TypeSafe Jev is measured and calibrated; Liquid d1 and custom models are supported but not validated.
- Held-out failures (not tuned against): `h-09`, `h-21`, `h-24` (docs/EVALUATION.md).

## Publishing (owner)

1. 👤 Review this checklist, the README, `git log main..v1` and the screenshots.
2. 👤 Push `v1`, let GitHub Actions run (Linux and Windows), and fix anything it finds.
3. 👤 Enable private vulnerability reporting (Settings > Code security).
4. 👤 Merge `v1` into `main`. Push the tags `v1.0.0` (and, optionally, the historical `v1.0.0-beta.1`). Create the GitHub release from docs/RELEASE_NOTES.md.
5. 👤 Deactivate old development keys that are no longer needed.
