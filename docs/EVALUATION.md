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

## Providers (added 2026-10-10, revised for the Beta)

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

## Results

All results come from TypeSafe `jev-latest` and are **historical**: they were measured on code from 2026-10-09 and 2026-10-10. The code has changed since then:
- the provider layer was rewritten;
- `interpret.py` was split into modules;
- arithmetic across aggregation levels and elapsed-time questions were added.

Those changes are covered by offline tests, but the Beta build itself has not been measured. The TypeSafe account returned HTTP 402 (no credits), and no Liquid key was available. Reports are in `../IntentSQL-artifacts/v1-runs/`.

| Evaluation | Data | Result | Report |
|---|---|---|---|
| Spider dev (validation) | 2 random batches of 300, seeds 15123661 / 89959258, 2026-10-09 | **76.8% ± 2.6 EX**. By hardness: easy 84.0, medium 80.6, hard 73.5, extra-hard 59.4 | `v2l-dev-random2x300.json` |
| Confident answers, same run | | 86.0% answered confidently, 86.0% of those correct; 12.0% of all questions answered confidently but wrongly | same |
| Accuracy by coverage, same run | | 90%: 84.7, 80%: 88.3, 70%: 91.7, 60%: 94.0, 50%: 94.9 | same |
| Spider train (development) | 300 questions each, seeds 21 / 31, 2026-10-09 | 76.3% / 75.3% | `v2m-train-300-s21/s31.json` |
| Capability suite | `features.json`, 56 cases, 2026-10-10 | 92.9% | |
| Adversarial suite | `adversarial.json`, 65 cases, 2026-10-10 | 95.4%; 5 of 6 unsupported requests refused, no false refusals | |
| Held-out suite | `holdout.json` | not run yet | |
| Spider test | ≥ 600 questions | not run yet | |

### Compared with the Alpha

| System | Data | EX |
|---|---|---|
| Alpha `v0.1.0-alpha.1` | Spider train, 80 questions, seed 11 | 33.8% (easy 68, medium 44, hard 0, extra-hard 0) |
| First v1 prototype (2026-10-09 21:22 UTC) | the same 80 questions | 58.8% |
| v1, three iterations later (21:28 UTC) | the same 80 questions | 77.5% |
| v1 (historical, above) | Spider dev, 600 questions | 76.8% |

The same-sample rows are the only like-for-like comparison. Even those flatter v1, because train questions were development data, and that sample was inspected while v1 was being built. The dev figure is the fairer estimate of v1's accuracy, but it is not comparable with the Alpha's train figure:
- different split;
- 600 questions against 80;
- different code.

Either way, the Alpha could not express joins beyond one hop, subqueries, set operations or arithmetic (docs/AUDIT.md). That accounts for its 0% on hard and extra-hard questions.

### To measure this Beta

```bash
intent-sql keys liquid            # or typesafe
STEPS="check smoke dev-suites" scripts/release_eval.sh liquid     # first look: ~140 questions, ~$0.07 at Jev's listed price
scripts/release_eval.sh liquid    # the full protocol, about $1
```
