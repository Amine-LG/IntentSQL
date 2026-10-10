# Changelog

## 1.0.0 (2026-10-10, not yet published)

The first stable release. 1.0.0 is 1.0.0-beta.1 plus the changes below; the Beta entry describes everything else that is new since the Alpha.

### Validated with TypeSafe Jev
- Validated live with TypeSafe `jev-1.13.0` on frozen code:
  - held-out suite: 93.3% (42/45);
  - Spider test sample (public labels): 78.0%;
  - Spider dev: 71.3% on a fresh sample, and 76.7% on the questions of the historical run, so no regression.

  docs/EVALUATION.md has every run, with seeds and costs.
- The calibrated confidence was rechecked on 600 fresh live answers and kept unchanged.
- Jev is the recommended model in the setup dialog.

### Look and feel
- The Alpha's visual identity is back on the 1.0 layout:
  - the green-black and lime studio palette (default), and cream with forest green in light mode;
  - mono micro-labels, the "The query, made clear." headline, the pipeline stepper with a stats strip, and numbered examples.
- The decision inspector now uses the Alpha's numbered timeline: the exact question asked, CHOICE/NOUL pills, the chosen option and probability bars.
- The layout uses the whole screen:
  - on wide screens, the question box sits beside the examples, and the answer beside a sticky decision timeline;
  - narrower screens stack;
  - the pipeline is shown from the start, with what each step does.

  Checked in a browser from 360 to 2560 px, at 125% and 200% zoom, in light and dark.
- Read-only made visible: a note under the question box; the SQL console explains refused writes.
- Other interface changes:
  - a one-click theme toggle;
  - a developer view in the inspector;
  - the connection status in the sidebar.

### Fixes
- Ordering by elapsed time ("longest-serving first") used values rounded for display, so close dates tied. It now orders by the exact value.

### Spending guard
- Each request attempt, retries included, reserves an upper bound of its cost and is refused if it could pass `INTENTSQL_BUDGET_USD`. Timeouts and server errors count as billed.
- `INTENTSQL_SPEND_LEDGER` keeps the totals in a file, so the limit is cumulative across runs and processes.

## 1.0.0-beta.1 (2026-10-10, local tag only)

A rewrite of the Alpha (`v0.1.0-alpha.1`, published `main`). Python package version `1.0.0b1`.

### Architecture
- **Grounding.** Deterministic. Its sources:
  - schema introspection, including inferred, composite and self-referencing foreign keys, role tables and subtypes;
  - stored-value literals, dates and periods, demonyms, and names split across columns;
  - lexical links between request words and tables or columns.
- **Interpretation.** One parallel Jev round of bounded Choices and Nouls over candidates that code proposes, with at most two targeted follow-up rounds. Jev never writes SQL or names an identifier.
- **Typed semantic plan (`Intent`) → typed SQL IR → compiler.** Values are bound as parameters, and plans are validated before they run.
- **Verification.**
  - Result-distinct alternative readings.
  - One parallel Jev check of the plain-English readings against their result previews.
  - Calibrated confidence, with uncertain answers labelled.
  - Deterministic explanations of empty results.

### SQL coverage
- Multi-hop, self and outer joins.
- Semi-joins and anti-joins, NULL-safe.
- INTERSECT and EXCEPT for both / never / always / but not.
- Correlated and scalar subqueries, including comparisons with a described subset.
- Nested aggregates and percentages.
- Derived measures (products, differences, durations) and column-to-column comparisons.
- Top-N per group (window functions).
- Year, month and quarter handling and relative periods; grouping by period.

### Application
- **Web UI rebuilt.** It shows:
  - what was asked, what IntentSQL understood, other readings, the result, the SQL, and how the answer was reached;
  - live progress, example questions generated from the schema, and history;
  - CSV export and copy, light and dark themes, a mobile layout, and a first-run connection setup.
- **Read-only.** Requests to change data are refused (the Alpha's write path is gone).
- **CLI.** `intent-sql ask | schema | benchmark | serve`.

### Providers and model setup
- Two supported decision models with the same question protocol: TypeSafe Jev (`jev-latest`) and Liquid AI d1 (`d1`, `d1:free`). Demo mode (formerly "mock") is used only when chosen, never as a fallback.
- First-run setup in the app. Connection states: not configured, ready to test, connected (only after a real answer), key rejected, no credits, unavailable, incompatible, demo, locked by `INTENTSQL_PROVIDER`.
- Keys are kept server-side (owner-only file), never returned to the page. `intent-sql keys <provider>` and `intent-sql provider --test`.
- Provenance (provider, model, request id, cost) on every exchange; spending limit; per-provider calibration.
- **Custom decision models.** Any endpoint that implements the System One protocol can be added, tested, selected, edited and removed, in the app or with `intent-sql custom`, without code changes.
  - Custom models are grouped apart from the built-ins and labelled "not verified by IntentSQL".
  - The connection test sends one question per declared decision type. Wrong-format replies are reported as *Incompatible*, naming what was wrong.
  - HTTPS is required except on localhost. A saved key is dropped when the endpoint's scheme, host or port changes.
- OpenRouter and OpenJEV, used during development, were removed: both development keys were rejected and neither produced any reported result.

### Decision inspector
- "How IntentSQL decided": what grounding found; each decision as asked, with its options, the model's probabilities and the top alternatives; the plan and its joins; the verification and the readings compared; the SQL with bound values. Raw JSON only in developer mode.
- Per-decision probabilities are labelled as such and kept apart from calibrated overall confidence. Demo answers show no probabilities.

### Release engineering
- CI: tests on Linux and Windows with Python 3.10 to 3.13; build, clean install and a CLI/API smoke test; Docker; gitleaks over the full history; pip-audit. No secrets and no paid calls.
- Security audit (docs/SECURITY.md); `.gitleaks.toml`; optional pre-commit hooks.
- Example databases ship with their CC BY-NC-SA notice.

### Evaluation
- **Spider pipeline.**
  - Uses train, dev and test splits with recorded seeds, random batches and the hardness breakdown.
  - Reports confidence and coverage curves.
  - Output for the test split is redacted.
- **Hand-written suites.** A feature suite, an adversarial suite, and a frozen held-out suite on an unseen schema.
- **Records.** The evaluation protocol (`docs/EVALUATION.md`) and the experiment log (`docs/EXPERIMENTS.md`), negative results included.
- **Results.** Historical, TypeSafe `jev-latest`, code from 2026-10-09: Spider dev 76.8% ± 2.6 execution accuracy, against the Alpha's 33.8% on 80 train questions. The two are not directly comparable; see docs/EVALUATION.md#results. This build has not been measured live.
