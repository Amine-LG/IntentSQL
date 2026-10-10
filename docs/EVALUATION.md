# Evaluation protocol

This protocol was frozen on 2026-10-10, before the final release assessment. It says which data may be used for what, how results are computed, and what is reported. Changing it requires a dated note at the end of this file.

## Data and what it may be used for

| Data | Role | May be inspected case by case? | May be tuned against? |
|---|---|---|---|
| Spider **train** (7,000 questions, 140 databases) | Development: failure analysis, regression checks | Yes | Yes |
| Spider **dev** (1,034 questions, 20 databases) | Validation at checkpoints | No: aggregates only | No |
| Spider **test** (2,147 questions, 40 databases) | Held-out milestone evaluation | No: per-case output is redacted by the CLI | No |
| `benchmarks/suites/features.json` (retail schema) | Development: one or more cases per SQL capability | Yes | Yes |
| `benchmarks/suites/adversarial.json` (retail schema) | Development: tricky wording, ambiguity, refusals | Yes | Yes |
| `benchmarks/suites/holdout.json` (clinic schema) | Held-out hand-written suite, written before the system ever ran on that schema | Failures may be listed in reports | No |

Spider test labels are the public 2023 release of the Spider 1.0 test set. That set is public, so "held out" here means that we never use it for development, not that it is secret. It is run only at milestones, with `--allow-test`, and the CLI prints aggregates only.

No benchmark-specific answers, templates or database names appear in the code. Cue words and schema heuristics are written for general English and generic schemas. Whenever one was motivated by a train failure, it was checked on fresh random train samples, not on the failing case alone.

## Scoring

- **Execution accuracy (EX).** The predicted and gold queries are run on the same database and their results compared as multisets. Row order counts only when the gold query orders its results. Columns may come in any order, and floats are compared after rounding to 6 decimals. Spider's own gold errors are kept as they are and are counted against us.
- **Status.** Every request ends as one of four statuses:
  - *answered*: shown as confident;
  - *ambiguous*: shown, but flagged as uncertain and given alternatives;
  - *refused*;
  - *error*.

  EX counts the SQL of ambiguous predictions too, because that SQL is what the user sees first.
- **Coverage and selective accuracy.**
  - *Answered rate*: the share of requests answered confidently.
  - *Accuracy when answered*: the accuracy of those confident answers.
  - *Wrong-answer rate*: confident answers that are wrong, as a share of all requests. This is the false-confidence rate.
  - *Accuracy-at-coverage curves*: requests ranked by verification confidence.
- **Refusals.** Hand-written suites report:
  - correct refusals of unsupported requests;
  - false refusals of answerable ones.
- **Paraphrase groups.** A group passes only if every phrasing in it is answered correctly.
- **Cost.** Reported per question:
  - Jev calls;
  - input and output tokens;
  - latency p50 and p95, including SQLite execution.
- **Uncertainty.**
  - Random batches report the mean and standard deviation across batches.
  - Single batches of n cases carry a binomial 95% interval of about ±1.96·√(p(1−p)/n). For n = 300 at 75%, that is ±4.9 points.
  - Uncached reruns of one fixed sample differ by about ±0.5 to 1 point, because Jev is not fully deterministic.

## Procedure for the release assessment

1. Run the development suites (`features`, `adversarial`) and fresh random train batches. These are for information only.
2. Run Spider dev, two fresh random batches of 300 with recorded seeds, and report aggregates.
3. Run the held-out suite once:
   `intent-sql benchmark --suite benchmarks/suites/holdout.json --show-failures`.
4. Run the Spider test milestone once, with stratified random batches covering at least 600 questions, recorded seeds and `--allow-test`.
5. Report all of these numbers, including the ones that went down. No change may be made between steps 3–4 and the report.

Every run writes a JSON report containing:
- the seeds;
- the strategy;
- per-case results (redacted for test);
- the summary statistics.

The reports live outside the repository, in `../IntentSQL-artifacts/v1-runs/`.

## Providers (added 2026-10-10, revised for 1.0)

The supported decision models are TypeSafe Jev (`jev-latest`) and Liquid AI d1 (`d1`, `d1:free`). Both use the same System One question protocol, but they are different models and are **never** assumed to be interchangeable:

- **One provider per run.** Every evaluation runs against a single provider. Its report records:
  - the provider;
  - the model that answered each request;
  - the code revision.
- **No fallback mid-run.** A run stops with `INTERRUPTED` and writes no report when the provider fails fatally:
  - a rejected key;
  - no credits;
  - a spending limit;
  - an outage that outlasts the retries.

  A partial run is never completed with another provider.
- **Separate results.** Results are always reported per provider.
- **Calibration is per provider.** The only calibration table was fitted on TypeSafe `jev-latest` train runs. Liquid d1 reports the raw verification score until its own table is fitted (`scripts/fit_calibration.py`) from its train runs.
- **Spending.** `scripts/release_eval.sh <typesafe|liquid>` runs the protocol in order, under `INTENTSQL_BUDGET_USD` (default $3). Jev lists $0.042 per million input tokens, so the full plan of about 2,000 questions costs roughly $1. Liquid does not report a per-request cost, so IntentSQL estimates spend from input tokens at its listed price.

Earlier development also used OpenRouter's and OpenJEV's access to Jev. On 2026-10-10 both keys were rejected (expired / invalid), and neither produced any of the results below. Both were removed for the Beta.

## Results: 1.0.0 release assessment (2026-10-10)

- **Model.** TypeSafe `jev-latest`, answered by `jev-1.13.0` (recorded in every report).
- **Code.** Frozen at commit `e2cef0c` for everything below the smoke test. The later commits change only the user interface, documentation and packaging, not the pipeline.
- **Calibration.** The table fitted on TypeSafe train runs, unchanged. It was checked on the 600 fresh train answers below: every confidence bin was within 3 points of its accuracy.
- **Reports.** In `../IntentSQL-artifacts/v1-runs/stable/`.

| Evaluation | Data | Result |
|---|---|---|
| Live smoke | `smoke.json`, 15 cases, one per SQL category | **15/15** |
| Capability suite (development) | `features.json`, 56 cases | **94.6%** (historical 92.9%) |
| Adversarial suite (development) | `adversarial.json`, 65 cases | **92.3%**; 5 of 6 unsupported requests refused, no false refusals (historical 95.4%) |
| Regression, same questions | Spider train, the 2 × 300 questions of the historical seeds 21 and 31 | **76.8%** (76.3 + 77.3), historical 75.8%; question-level changes: 3 → 3 and 0 → 6 (wrong → right) |
| Regression, same questions | Spider dev, the 600 questions of the historical headline run (seeds 15123661, 89959258) | **76.7%**, historical 76.8%; confident answers 85.2% correct |
| **Held-out suite** | `holdout.json`, 45 cases on a clinic schema never used in development; run once on frozen code | **42/45 = 93.3%** (Wilson 95% interval 82–98%); 3/3 unsupported requests refused; 39/40 confident answers correct |
| Spider dev, fresh sample | 2 random batches of 300, seeds 1968554267, 447747827 | **71.3% ± 0.9**; confident answers 81.5% correct (82.8% answered); 87.9% at 70% coverage; extra-hard 62.5% |
| **Spider test** | 2 random batches of 300, seeds 988774448, 2099190301; public 2023 test labels; databases from the test split, never used in development | **78.0% ± 2.4**; confident answers 87.5% correct (85.3% answered); 90.1% at 70% coverage; by hardness easy 84.9, medium 83.6, hard 70.8, extra-hard 65.0 |
| Cost | all runs above | 4,203 billed requests, 18.77M input tokens, **$0.79** at the listed $0.042/M |

**Reading the dev numbers.** The two dev samples differ by 5.4 points: 76.7% on the historical questions and 71.3% on a fresh draw, with the same code and model. The same-question comparison shows no regression. The gap is the variance between 600-question samples of a 1,034-question split; a fresh sample is the less biased estimate. Together they suggest Spider dev accuracy in the low-to-mid 70s.

**Spider test is not an official score.** It uses the public 2023 test labels on a stratified random sample, scored by IntentSQL's own execution matcher (`docs/EVALUATION.md#scoring`), not the official evaluation script or leaderboard.

**Caching.** Runs used a response cache keyed by the exact request. Repeated identical requests were answered from the cache, mainly in the same-question dev run after the fresh dev run. Cached answers are the model's real earlier answers to the same request.

**Held-out failures (not tuned against):**
- `h-09`: "Which doctors work in Cardiology?" also filtered on specialty.
- `h-21`: a computed cost column was missing.
- `h-24`: patient IDs instead of names, flagged uncertain.

**Development regression found and fixed before freezing.** Ordering by elapsed time ("longest-serving first") sorted on values rounded for display, so close dates tied (adversarial `ord-2`). The fix is commit `e2cef0c`, with a regression test.

### Historical results (code from 2026-10-09)

| Evaluation | Result |
|---|---|
| Spider dev, 2 × 300 (seeds 15123661, 89959258) | 76.8% ± 2.6; confident 86.0%; 91.7% at 70% coverage; extra-hard 59.4% |
| Spider train, seeds 21 / 31 | 76.3% / 75.3% |
| Capability / adversarial suites | 92.9% / 95.4% |

### Compared with the Alpha

| System | Data | EX |
|---|---|---|
| Alpha `v0.1.0-alpha.1` | Spider train, 80 questions, seed 11 | 33.8% (easy 68, medium 44, hard 0, extra-hard 0) |
| First v1 prototype (2026-10-09 21:22 UTC) | the same 80 questions | 58.8% |
| v1, three iterations later (21:28 UTC) | the same 80 questions | 77.5% |
| 1.0.0 | Spider train, 600 questions | 76.8% |
| 1.0.0 | Spider test, 600 questions (public labels) | 78.0% |

The same-sample rows are the only like-for-like comparison with the Alpha, and they flatter v1: train questions were development data, and that sample was inspected during development. The Alpha could not express joins beyond one hop, subqueries, set operations or arithmetic (docs/AUDIT.md), which accounts for its 0% on hard and extra-hard questions.

### Re-running

```bash
intent-sql keys typesafe
export INTENTSQL_BUDGET_USD=1 INTENTSQL_SPEND_LEDGER=~/intentsql-spend.json   # cumulative cap across runs
STEPS="check smoke dev-suites" scripts/release_eval.sh typesafe                 # about $0.10
scripts/release_eval.sh typesafe                                                # the full protocol, about $1
```
