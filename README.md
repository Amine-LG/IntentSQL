# IntentSQL

**The query, made clear.** Ask a SQLite database a question in plain English. IntentSQL shows what it understood, every decision behind the SQL, the read-only query it ran, and the result.

![IntentSQL answering "Which five districts have the highest per-pupil expenditure?" with TypeSafe Jev](docs/images/answer-jev.png)
<sub>A real answer from TypeSafe Jev (`jev-1.13.0`): two model calls, 10,678 input tokens, about $0.0005. Jev's check was unsure this time, so the answer is flagged *Uncertain* and the other reading is offered beside it.</sub>

## Why it works this way

Language models write plausible SQL that can be subtly wrong, and it is hard to see why. IntentSQL never lets a model write SQL. **Code proposes, the model chooses, and code builds:**

1. **Grounding.** Code reads the database and your question. It finds the values you mention (names, numbers, dates, periods) and the tables and columns they belong to.
2. **Bounded decisions.** Code builds every reasonable option: which column to show, which value filters which column, how to group, sort or join. A *decision model* answers small multiple-choice questions about which option you meant, in one parallel round.
3. **Typed interpretation.** The answers are assembled into a typed plan and described back to you in plain English.
4. **Deterministic SQL.** A compiler turns the plan into parameterized SQLite. Joins come from the database's keys, and every value is a bound parameter.
5. **Verification.** Readings that differ by one decision *and give different results* are run too. The model checks the plain-English readings against your question. Uncertain answers are flagged, with the alternatives offered.
6. **Read-only results.** Queries run on a read-only connection with row and time limits. Requests to change data are refused.

Every step is visible. **How IntentSQL decided** is a timeline of the decisions, each showing:
- the exact question asked;
- the option chosen;
- the model's probability for each option.

It also shows the values found, the joins and plan, the verification, and the SQL. Per-decision probabilities are not the chance that the answer is right. That is the separate, calibrated confidence on the answer.

![The answer beside its decision timeline: exact questions, choices and Jev's probabilities](docs/images/decisions-jev.png)
<sub>The same answer, scrolled: results and SQL on the left; on the right, each decision with the exact question Jev received and its probabilities.</sub>

## What it supports

- filters, ranges, text matching, NULLs and DISTINCT;
- multi-hop, self- and outer joins over declared and inferred keys;
- aggregates, grouping, HAVING, COUNT(DISTINCT), nested aggregates and percentages;
- derived measures (quantity × price, durations, elapsed time) and column-to-column comparisons;
- "never" / "both" / "always" (anti-joins, INTERSECT, EXCEPT), correlated and scalar subqueries;
- top-N per group, date parts, quarters and relative periods.

Each feature is listed with examples, and with what is **not** supported, in [docs/CAPABILITIES.md](docs/CAPABILITIES.md).

**Read-only.** IntentSQL only reads data. It never adds, changes or deletes records, and it works on a copy of each database you open, so the original file is never touched.

## What changed since the Alpha

[`v0.1.0-alpha.1`](https://github.com/Amine-LG/IntentSQL/tree/v0.1.0-alpha.1) was an experiment in letting a decision model steer a query planner, one stage at a time. 1.0 rebuilds the core and keeps what worked: bounded questions, exact SQL, and visible decisions.

| | Alpha (v0.1.0-alpha.1) | 1.0 |
|---|---|---|
| **Interpretation** · rebuilt | A chain of planner "skills", each asking Jev about one stage, with code-defined repair loops | Code builds every candidate reading up front; Jev answers one parallel round of bounded questions; deterministic assembly into a typed plan |
| **SQL coverage** · much broader | One foreign-key hop, flat AND/OR, up to two grouping keys and one HAVING, scalar aggregates | Multi-hop, self- and outer joins; correlated, scalar and FROM subqueries; INTERSECT/EXCEPT; anti-joins; nested aggregates; percentages |
| **Arithmetic and dates** · new / improved | Date ranges and rounding; no arithmetic | Derived measures (quantity × price, differences, durations, elapsed time), date parts, quarters, relative periods |
| **Top-N per group** · new | Per-group MIN/MAX only; ranking requests refused | "The top 3 in each…" via window functions |
| **Checking the answer** · new / improved | A whole-request coverage review | Readings that differ by one decision *and give different results* are run and compared; Jev checks each plain-English reading against its results |
| **Confidence** · new | Jev's raw per-choice confidence | Confidence calibrated on measured accuracy; uncertain answers flagged, with the alternatives |
| **Decision inspector** · improved | Exact questions, probabilities and raw exchanges, as a list of calls | The same honest data as a staged timeline (found → decided → planned → checked → SQL), beside the result on wide screens |
| **Model setup** · improved | Connection settings for Jev, OpenJEV, a local Laya adapter, or a custom endpoint | Guided first run; *Connected* only after a real answer; custom System One endpoints tested for compatibility; never a silent fallback |
| **Install and tools** · new / kept | Run from a clone with uvicorn; Docker | A pip package with an `intent-sql` CLI (`ask`, `schema`, `benchmark`, `keys`, `custom`); Docker kept |
| **Engineering** · improved | Unit tests and CI (tests, JS check) | 152 offline tests, ruff, mypy, clean-install and Docker CI jobs, secret scanning, dependency audit, a cumulative spending guard |
| **Writing data** · removed | INSERT/UPDATE/DELETE with preview, confirmation and undo | Deliberately left out of 1.0: IntentSQL only reads |

## Install and run

Python 3.10 or newer:

```bash
git clone https://github.com/Amine-LG/IntentSQL.git && cd IntentSQL
python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/intent-sql serve            # http://127.0.0.1:7862
```

Or Docker. Publish the port on localhost only, because the app stores your model key:

```bash
docker build -t intentsql . && docker run --rm -p 127.0.0.1:7862:7862 -v intentsql-data:/data intentsql
```

Three example databases are included: Cyberchase, DESE and Moneyball. **Open database** imports your own SQLite file; IntentSQL works on a copy and never changes the original.

## Connect a model

On first start, IntentSQL asks you to choose a decision model. Nothing is chosen silently, and it never falls back to demo mode on its own.

![Model setup: built-in models, your own models, and demo mode](docs/images/model-setup.png)

| | Model | Status |
|---|---|---|
| **TypeSafe Jev** (recommended) | `jev-latest` | Validated live for this release ([results](#results)). Get a key at [console.typesafe.ai](https://console.typesafe.ai); $0.042 per million input tokens |
| **Liquid AI d1** | `d1`, `d1:free` | Implemented and tested offline; not yet validated with real answers |
| **Your own model** | any | Any endpoint that speaks the System One decision protocol. See [docs/CUSTOM_MODELS.md](docs/CUSTOM_MODELS.md) |
| **Demo mode** | none | Fixed default choices, so you can explore the app. It does not understand questions |

**In the app:**
1. Click the model button at the top right.
2. Paste a key and click **Save and use**.
3. Click **Test connection**. This sends one tiny question of each decision type.

The model shows *Connected* only after a real answer. Otherwise it shows the provider's own message: *Key rejected*, *No credits*, *Unavailable* or *Incompatible*.

**On the command line:**

```bash
intent-sql keys typesafe          # key typed without echo
intent-sql provider --test
intent-sql custom add "My model" --url https://models.example.com/v1/systemone --model my-model --key --use
```

Keys stay server-side in an owner-only settings file and are sent only to their own provider. They are never returned to the browser or written to logs and reports. `INTENTSQL_BUDGET_USD` sets a spending limit; with `INTENTSQL_SPEND_LEDGER` that limit holds across runs.

## Command line

```bash
intent-sql ask dese.db "Which five districts have the highest per-pupil expenditure?"
intent-sql ask dese.db "How many schools does each district have?" --json --debug
intent-sql schema moneyball.db
```

## Results

Measured for this release with TypeSafe `jev-1.13.0` on frozen code, using the protocol in [docs/EVALUATION.md](docs/EVALUATION.md):

| Evaluation | Result |
|---|---|
| Held-out suite: 45 questions on a clinic schema never used in development, run once | **93.3%** (42/45; 95% interval 82–98%) |
| Spider test: 600 random questions, public labels, test-split databases | **78.0% ± 2.4**; when IntentSQL is confident (85% of questions), **87.5%** are right |
| Spider dev: 600 random questions (fresh sample) | **71.3% ± 0.9**. On the 600 questions of the historical run: 76.7% (historically 76.8%) |
| Capability / adversarial suites | 94.6% / 92.3%, with unsupported requests refused and no false refusals |
| Cost | about 2 model calls and 8,000 input tokens per question: roughly $0.0004 |

What the numbers mean:
- Between one in eight (Spider test) and one in five (fresh Spider dev sample) confident answers are still wrong. Read the "IntentSQL understood" line.
- The hardest questions remain the weakest area: extra-hard Spider questions score 62–65%.
- The Spider test score uses IntentSQL's own execution matcher on a random sample. It is not an official leaderboard result.

### From the Alpha to 1.0, in numbers

Spider execution accuracy, all with TypeSafe Jev:

| | Questions | Accuracy |
|---|---|---|
| Alpha `v0.1.0-alpha.1` | 80 Spider train questions (seed 11) | **33.8%**; 0% on hard and extra-hard |
| Early rewrite (2026-10-09) | the same 80 questions | **77.5%** |
| 1.0 | 600 Spider train questions (seeds 21, 31) | **76.8%** |
| 1.0 | 600 Spider dev questions (fresh sample) | **71.3%** |
| 1.0 | 600 Spider test questions (public labels) | **78.0%** |

Only the first two rows use the same questions. Even that comparison flatters the rewrite: those train questions were development data, inspected while it was built. The 1.0 rows are different samples from different splits, so read the table as a clear trend, not a controlled head-to-head. The Alpha could not express multi-hop joins, subqueries, set operations or arithmetic at all, which is where most of the gap on hard questions comes from. Every run, with seeds and caveats, is in [docs/EVALUATION.md](docs/EVALUATION.md#compared-with-the-alpha).

## Limitations

- Read-only by design: no inserts, updates or deletes in 1.0.
- SQLite only, one question at a time, and no follow-up questions about a previous answer.
- Accuracy depends on the model. Only TypeSafe Jev is measured and calibrated. Liquid d1 and custom models show the raw verification score until they are measured.
- Ambiguous wording can produce a confident wrong answer. Alternatives are offered when readings disagree, and they are worth reading.
- [docs/CAPABILITIES.md](docs/CAPABILITIES.md) lists the SQL features that are not supported.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt -e .
.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy intentsql
scripts/smoke.sh                       # CLI, API and custom models; no key, no network
scripts/release_eval.sh typesafe       # the live evaluation protocol (spends credits)
```

CI (`.github/workflows/ci.yml`) runs:
- tests, lint and type checks;
- a clean install with a smoke test;
- a Docker build;
- a secret scan of the full history;
- a dependency audit.

It never calls a paid model. The architecture is described in [ARCHITECTURE.md](ARCHITECTURE.md), and security in [docs/SECURITY.md](docs/SECURITY.md).

## License

The code is under the [MIT license](LICENSE). The bundled `cyberchase.db`, `dese.db` and `moneyball.db` come from [Harvard CS50's Introduction to Databases with SQL](https://cs50.harvard.edu/sql/). They are under its [CC BY-NC-SA 4.0 license](https://cs50.harvard.edu/sql/license/), separately from the code ([notice](intentsql/data/NOTICE.md)).
