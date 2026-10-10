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

## Providers (added 2026-10-10)

The same Jev model can be reached through several providers (TypeSafe direct, OpenRouter, OpenJEV). They are **not** assumed to be identical:

- **One provider per run.** Every evaluation runs against a single provider. Its report records the provider, the model snapshot that answered each request, and the code revision.
- **No fallback mid-run.** If a provider fails fatally (expired key, no credits, spending limit, an outage that outlasts the retries), the run stops with `INTERRUPTED` and writes no report. A partial run is never completed with another provider.
- **Separate results.** Results are reported per provider, for example "Spider dev, OpenRouter typesafe/jev-1.13: …".
- **Calibration is per provider.** The current table was measured with TypeSafe direct `jev-latest` on train. A provider without its own table reports the raw verification score and uses the raw "uncertain" threshold. Before any of its confident-answer figures are compared, a table is fitted on train runs for that provider.
- **Spending.** `scripts/release_eval.sh <provider>` runs the protocol steps in order under `INTENTSQL_BUDGET_USD`, which defaults to $3. At the listed $0.042 per million input tokens, the full plan of about 2,000 questions costs roughly $1.
