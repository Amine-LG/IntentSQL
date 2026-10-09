# Changelog

## 1.0.0 (unreleased, local)

A rewrite of the Alpha (`v0.1.0-alpha.1`, published `main`).

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

### Evaluation
- **Spider pipeline.**
  - Uses train, dev and test splits with recorded seeds, random batches and the hardness breakdown.
  - Reports confidence and coverage curves.
  - Output for the test split is redacted.
- **Hand-written suites.** A feature suite, an adversarial suite, and a frozen held-out suite on an unseen schema.
- **Records.** The evaluation protocol (`docs/EVALUATION.md`) and the experiment log (`docs/EXPERIMENTS.md`), negative results included.
