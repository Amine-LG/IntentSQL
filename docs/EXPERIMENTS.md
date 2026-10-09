# Jev interaction experiments

All experiments use the Spider **train** split (development data), 300-case stratified samples (seeds 21 and 31), and test-suite-style execution accuracy (EX). Uncached reruns of the same configuration vary by about ±0.5 points, so differences under ~1.5 points are treated as noise. Dev and test were not used to choose any of these settings.

## Architecture-level findings

| # | Question | Result | Decision |
|---|---|---|---|
| A1 | Independent yes/no (Noul) per output candidate | Near-synonyms all score high: "number of singers" 0.94, "number of distinct singer ids" 0.89 | Exclusive alternatives compete in **one Choice** |
| A2 | Flat literal-role Choice (~200 column×operator options) | "at least two courses" was read as `course_id >= 2` | **Hierarchical speculative fan-out**: role → column → operator, all asked in the same request; only the selected branch is used |
| A3 | Round-1 fan-out latency | 18 questions in 0.45 s; 100–300 questions in ~0.35–0.6 s (parallel, chunked) | One wide round, not many sequential calls |
| A4 | Jev determinism | Identical requests: probabilities differ by ~0.03; co-batched questions shift them slightly | Benchmarks quote ±0.5 pt noise; decisions near 0.5 are treated as uncertain |

## Prompt and context experiments

| # | Variant | Seed 31 | Seed 21 | Decision |
|---|---|---:|---:|---|
| E1 | No schema listing in the state | 59.3 (−11.7) | — | Keep the schema listing |
| E2 | Ask every Choice twice with reversed options, average (first-option bias) | 71.0 (±0), +73% tokens | — | Rejected |
| E3a | Sample stored values in the schema listing | 70.0 (−1.0) | — | Rejected (noise level) |
| E3b | FK relationships in the state | 71.0 (±0) | — | Rejected |
| E5 | Three phrasings of the relationship (has / lacks) question | 14/16 each on a labelled probe set | — | Kept v1; misses are boundary noise |
| E6 | Output questions against an extracted "asked for" phrase | 71.3 (+0.3) | 71.3 (−3.4) | Rejected |
| E8 | Column phrases without the owning entity ("per pupil expenditure") | 70.0 (−2.0) | 74.7 (−0.3) | Rejected: always keep the entity |
| E9 | Singular entity names ("customer name", "customer records") | 71.0 (±0) | 74.7 (±0) | Kept (clearer, neutral) |

## Plan-level selection and verification

**E4, oracle for reranking:** re-run the deterministic assembly with one low-margin decision flipped to its runner-up option. Base 71.0%, oracle@one-flip 74.7%, 0.53 alternatives per query. Jev is mostly *confidently* wrong, so reranking near-ties alone has a low ceiling.

**E7, can a plan-level check predict wrong answers?** Signals scored by AUC (correct vs wrong):

| Signal | AUC | Accuracy at 100 / 90 / 80 / 70 / 50% coverage |
|---|---:|---|
| Minimum decision margin | 0.70 | 71.7 / 73.8 / 76.1 / 78.8 / 84.5 |
| Verification Noul on the English interpretation | 0.77 | 71.7 / 76.4 / 79.8 / 83.7 / 87.8 |
| Same, with row count and first rows as evidence | **0.79** | 71.7 / 77.2 / 79.8 / 83.7 / 87.8 |

**Adopted: verification round.** The main reading and its result-distinct one-flip alternatives are verified in one parallel call. A comparative Choice can switch to an alternative only by a clear margin. Low verification (< 0.2) is reported as *uncertain* rather than answered.

| | EX | Confident answers | Accuracy when confident |
|---|---:|---:|---:|
| Seed 31 | 71.0 → **73.0** | 89.7% | 78.1% |
| Seed 21 | 74.7 → 74.7 | 88.0% | 81.8% |

**E10, which verification question separates right from wrong readings best?** These were scored on the labelled interpretations from E7 (train, seed 31; `experiments/verify_prompts.py`).

| Variant | AUC | Accuracy at 90 / 80 / 70% coverage |
|---|---:|---|
| v0 "describes exactly what the request asks, nothing missing, nothing extra" | **0.772** | 76.4 / 79.4 / 82.7 |
| v1 "would answering it give the person what they asked for" | 0.734 | 75.7 / 79.4 / 79.3 |
| v2 two Nouls, "missing" × "extra" | 0.751 | 75.7 / 79.0 / 81.7 |
| v3 4-level Score rubric | 0.769 | 75.3 / 79.8 / 82.7 |

Rerun on 2026-10-10 with the same labelled set. v0 was kept: it is as good as the rubric, with one Noul and no rubric to maintain.

**E11, how the reading is phrased for verification.** The first renderer wrote "Return the X of Y where …". Jev read it literally and scored correct readings at about 0.15. A natural phrasing ("List the name of each region that has no stores", "Count the orders whose status is 'cancelled'") raised correct readings to about 0.9 while wrong readings stayed low. It is used both for verification and as the explanation users see (`render.py`).

## v2 iteration (2026-10-09 to 10)

Runs use train seeds 31 and 21 (300 cases each) unless noted. Per-seed noise is about ±1 point.

| # | Change | Seeds 31 / 21 | Other evidence | Decision |
|---|---|---|---|---|
| E12 | Quantified conditions (never / always / both), subset aggregates, derived measures, column comparisons, composite FKs | 73.7/77.3 → 74.3/76.3 | 3 targeted cases fixed, no systematic loss | Kept: new capabilities at no cost |
| E13 | Self-reference role tables, possessive tokenization | 74.7/77.0 | adversarial 89.2 → 93.8% | Kept |
| E14 | Role tables for *parallel* references too (source/destination airport as separate tables) | not run | — | Not adopted: the first parallel reference would have to stay a plain join, which makes the base table silently mean one role. The per-request relationship choice is kept; `INTENTSQL_PARALLEL_ROLES=1` enables the experiment |
| E15 | Filters through one-to-many relationships as membership tests (no repeated rows) | 74.0/77.0 → 75.0/76.7 | features: f-join-2 fixed | Kept: correct semantics, neutral on Spider |
| E16 | Aggregate comparisons only with a comparative word ("average order value" no longer gets 250 comparison options) | within noise | about −10k prompt characters on such requests | Kept |
| E17 | Calibration: verification score − 0.2 × result-distinct alternatives | AUC 0.742 → 0.765 (1,200 predictions) | accuracy-at-coverage improves at every threshold | Kept, mapped to observed accuracy (isotonic, train only) |
| E18 | Row identity from data statistics only in tables of ≥ 20 rows | 75.7/75.7 | fixes "editors per age" counting another table | Kept |
| E19 | Group labels show the identified record's name (supplier name for "per supplier"), even if this adds a join | neutral across v2e–v2i | fixes a bug that showed a product name | Kept: better answers, no measurable cost |

### Signals that predict a wrong answer (AUC, train, 1,200 predictions)

| Signal | AUC |
|---|---:|
| Verification probability | 0.742 |
| Minimum decision probability | 0.628 |
| Number of result-distinct alternatives (fewer = better) | 0.609 |
| Empty result | 0.500 (uninformative) |
| Verification − 0.2 × alternatives | **0.765** |

Accuracy by number of distinct alternatives:
- 0 alternatives: 80.5% (n = 966);
- 1 alternative: 58.2% (n = 194);
- 2 alternatives: 48.1% (n = 27);
- 3 alternatives: 27.3% (n = 11).
