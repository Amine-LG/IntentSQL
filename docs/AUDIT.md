# v1 audit: what the Alpha was, what it measured, what changes

Written at the start of the v1 rework (2026-10-09), before any runtime code was changed. This is a historical record; the current design is in [ARCHITECTURE.md](../ARCHITECTURE.md) and current measurements are in the README.

## Sources examined

- Published repository, `main` at `f615d3c` ("Initial Alpha release of IntentSQL"): the source of truth.
- Private archive (152 commits, 2026-09-24 → 09-30). Its working tree is identical to the published tree. Its value is in history: deleted handoff notes, per-sample Spider failure tables, and probes.
- External artifacts: historical Spider run JSONs, handoff/continuation notes and pre-IntentSQL "semantic selector" experiments.

## How the Alpha worked

1. A monolithic planner ("JevQL", `read_engine.py`, 5.5k lines) looped up to 24 steps. Each Jev call carried the full compact schema, the current program and the last 10 decisions. It then ran up to 3 vet/revise rounds. It was demoted because calls were large and gave the model many places to reinterpret earlier decisions.
2. It was replaced by a semantic graph router (`semantic_read.py` plus 40 `skills/`). A route call selected branches, and each branch asked small stage-local Choice/Noul questions. A whole-request *coverage* review gated execution, and finite repair paths handled objections.
3. Capability envelope:
   - one source table plus at most one declared-FK hop;
   - flat AND/OR;
   - ≤2 grouping keys and one HAVING;
   - scalar aggregates and per-group MIN/MAX.
   It had no joined aggregates, multi-hop joins, subqueries, set operations, arithmetic or anti-joins.

## Recorded lessons (from history and handoff notes)

- **The coverage reviewer acted as a second planner.** A broad "does this plan cover the request?" call contradicted established structure and produced most false refusals. Verification must be specific, never a free veto.
- **Duplicated semantic authority**, such as competing filter planners and repairs that re-planned, caused drift.
- **Recurring Jev confusions:**
  - a stored "pupil count" column vs `COUNT(*)`;
  - top-N vs global extremum;
  - "highest rank" read as the numerically largest value;
  - COUNT DISTINCT vs COUNT;
  - equality widened to LIKE.
- **Deterministic bugs:**
  - trusting declared types for AVG;
  - whitespace and case in literals;
  - evaluator bugs ("150" vs 150.0).
- Spider was only ever evaluated on a mechanically filtered single-table subset: 212 of 1,034 dev cases were eligible. The frozen final sample scored 30/40.

## Baseline measured for v1

The published Alpha read path was run on **80 stratified random Spider *train* cases** (seed 11; `scripts/baseline_alpha.py`). Train is the development split; the scoring is test-suite-style execution equivalence.

| Metric | Alpha |
| --- | ---: |
| Execution accuracy (all cases) | **33.8%** |
| Answered rate | 46.3% |
| Accuracy when answered | 73.0% |
| Wrong answers (share of all cases) | 12.5% |
| Easy / medium / hard / extra | 68% / 44% / 0% / 0% |
| Jev calls per query (mean) | 7.1 |
| Input tokens per query (mean) | 5,700 |
| Latency p50 / p95 | 2.45 s / 4.73 s |

Spider usage (feature frequency in train/dev/test):
- joins: about 40%;
- COUNT: about 40%;
- GROUP BY: about 27%;
- ORDER BY: about 23%;
- nested subqueries: about 8%;
- set operations: about 8%;
- HAVING: about 7%.

The Alpha's envelope excludes most of this, which explains the 0% hard/extra result.

## Decisions for v1

- **Rebuild the read path** around four pieces:
  - deterministic schema grounding;
  - a typed relational IR;
  - one deterministic compiler;
  - Jev decisions over *rendered, grounded candidates*.
  Joins are computed from the FK graph, never chosen as SQL.
- **No whole-request veto reviewer.** Verification compares concrete alternative plans (rendered in English and executed) and selects between them, or surfaces genuine ambiguity.
- **Data discipline for Spider:**
  - `train` is for development and failure inspection;
  - `dev` is for validation, using aggregate numbers only;
  - `test` is held out and evaluated only at milestones, never inspected per case.
- The Alpha's write path (INSERT/UPDATE/DELETE) depends on the legacy engine. It is out of scope for the v1 read architecture; see README for the final decision.
