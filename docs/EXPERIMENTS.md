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
