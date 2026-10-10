# IntentSQL architecture

IntentSQL turns a natural-language request about a SQLite database into one parameterized, read-only SQL query. The design rests on one rule: **code proposes, Jev chooses, and code builds.**

Every identifier, value, join path and SQL construct comes from deterministic code that inspected the database. Jev, TypeSafe's decision model, answers small, bounded questions about which of the proposed readings the request means. Jev never writes SQL, never names a column and never sees a free-text field it could fill.

```
request ──► grounding ──► candidates ──► Jev: one parallel round of bounded decisions
                                              │
                         Intent (semantic plan) ◄── deterministic assembly (+ ≤2 follow-up rounds)
                                              │
                         IR (typed SQL plan) ◄── deterministic builder: joins, subqueries, set ops
                                              │
                    compile + validate ──► run main reading and one-flip alternatives (read-only)
                                              │
                    verification: one parallel Jev call on the English readings + result previews
                                              │
                    answer / uncertain (alternatives shown) / refusal ──► result, SQL, explanation
```

## Modules

| Module | Responsibility |
|---|---|
| `schema.py` | Introspection: tables, columns, kinds, storage classes, value statistics, declared and inferred foreign keys (composite keys, `<table>_id`, role names, self-references), role tables, bridge detection, join trees, FK equivalence |
| `grounding.py` | Literals (quoted values, stored values, numbers, dates, periods, demonyms, fuzzy matches); lexical links from request words to tables and columns |
| `measures.py` | Derived columns that a request may mean but the schema does not store: products (quantity × price), differences and durations, sums, ratios, date periods (year, month, weekday) |
| `cues.py`, `candidates.py`, `questions.py` | Cue words; candidate generation (every option offered to Jev, built from the schema and literals); the bounded questions and shared decision state |
| `interpret.py` | The decision rounds, deterministic assembly of answers into an `Intent`, follow-up questions, one-flip alternatives |
| `providers.py`, `mock.py` | Jev providers (OpenRouter, TypeSafe direct, OpenJEV) and the offline mock |
| `intent.py` | The semantic plan (`Intent`, `OutputItem`, `Condition`, `Ordering`) and the deterministic translation to IR |
| `ir.py` | Typed relational IR: expressions, conditions, joins, FROM subqueries, window functions, set operations |
| `compiler.py` | IR → SQLite SQL with bound parameters; structural validation (unknown objects, aggregates in WHERE, set-width mismatches, unnamed derived columns …) |
| `render.py` | Plain-English description of an `Intent`: shown to users and compared with the request during verification |
| `pipeline.py` | `answer()`: the whole flow, verification, execution with row/time limits, empty-result explanations |
| `jev.py` | Jev client: chunking, parallel requests, validated answers, retries, usage accounting, optional disk cache |
| `web.py`, `static/` | Local web app and JSON API (including server-sent progress events) |
| `cli.py` | `intent-sql ask | schema | benchmark | serve` |
| `benchmark/` | Spider loading, stratified sampling, execution matching, reports, hand-written suites |

## Grounding

`ground()` is deterministic and runs in milliseconds on a cached schema.

- **Literals.** In order:
  - quoted strings;
  - ISO dates and natural dates ("June 21, 2002");
  - periods ("March 2024", "Q1 2024", "the last 30 days"; relative periods record the date they were resolved against);
  - the longest n-grams found among stored values, with singular forms and possessives;
  - numbers and number words;
  - ordinals that are not ranks;
  - capitalized spans, matched with fuzzy, containment and demonym lookups ("Japanese" → "Japan").

  A capitalized value the database does not contain is still kept as a literal, because dropping it would silently widen the answer.
- **Links.** For each table and column, the share of its label's content words found in the request. Links order the options in each question; Jev leans toward first options, so relevance ordering makes that lean harmless.
- **Derived measures.** These are proposed only when cue words and column names agree, for example "revenue" with a quantity column and a price column. They are added to a request-scoped copy of the schema, so the cached schema is never modified.

## Decisions (one round, all in parallel)

`round_one()` asks every decision a request might need, at once, over candidates rendered as short English phrases.

| Decision | Kind | Options come from |
|---|---|---|
| count | Choice | countable tables; distinct values when the request says "different"; average count per parent |
| output per column class | Choice | the value itself, AVG/SUM/MAX/MIN, nested per-parent totals, or "not shown" |
| literal role → column → operator | Choice ×3 | each literal: value / count threshold / group total / limit / compared-against subset / reference; columns storing or accepting the value; operators by type |
| exclusion / universality | Noul | literals when the request negates or says "always" / "only" |
| implicit categorical value | Choice | low-cardinality columns and two-valued flags |
| relationship | Choice | has / lacks for related tables |
| aggregate comparison | Choice | vs. overall average/max/min, vs. own group (correlated), most/least common value, more records than the average parent |
| column comparison | Choice | pairs of named columns, or an attribute of a record vs. the same attribute of its role (manager) |
| grouping, ordering, second ordering, limit, connector, scope | Choice | from the schema |

Alternatives that exclude each other always compete in one Choice, never as independent yes/no questions. Independent yes/no questions over-select near-synonyms (EXPERIMENTS A1). Dependent questions are asked speculatively for every parent option, and assembly reads only the branch the parent chose (A2). This keeps the whole interpretation to one call, at about 0.4 s.

## Assembly (deterministic)

`assemble()` turns answers into an `Intent`, applying rules that need no judgment:

- semantic plans:
  - filters through one-to-many relationships become membership tests;
  - negations on related records become anti-joins (NULL-safe on the referencing side);
  - "never" / "have not" with a non-identifying output becomes a set difference;
  - "always" becomes the difference with the negated condition;
- explicit "both", or two ranges no single row can satisfy, are read as two different rows (INTERSECT);
- one person's first and last name are one record; two people joined by "or" form a disjunction of conjunctions;
- grouping keys follow from outputs, groups are labelled by the identified record's name, and plain values that vary within a group are dropped;
- top-N within groups uses a window function; "including those with none" uses an outer join;
- counting under fan-out uses COUNT(DISTINCT key);
- FK-equivalent columns are rebound jointly to minimize the join tree;
- equal-length alternative join routes are resolved by the relationship the request names, otherwise asked in a follow-up.

When assembly lacks something it cannot decide, it asks a targeted follow-up rather than guessing, in at most two extra rounds. Follow-ups cover the main output when nothing was chosen, count versus total, a join route, and which condition defines a percentage.

## From Intent to SQL

`build_query()` derives the SQL structure:
- Steiner join trees over FK edges;
- IN / NOT IN subqueries;
- correlated scalar subqueries;
- keyed set operations (records are removed by key, so two customers named Ann Lee remain two rows);
- FROM subqueries for nested aggregates;
- window functions for per-group ranking.

`compile_query()` validates the IR against the schema and emits SQL with every value bound as a parameter. Results are read on a read-only connection (`query_only`), with a row cap and a time limit.

## Verification

Verification uses three kinds of evidence:

1. **Deterministic.** The compiler rejects impossible plans. Unsatisfiable conjunctions are repaired into two-row readings. When a query returns nothing, each condition is relaxed in turn to report which one removes every row, along with the date range the data covers.
2. **Alternatives.** Each low-margin decision is flipped to its runner-up and the same assembly is re-run. Only readings that produce *different results* are kept.
3. **Jev verification, in one parallel call.** The English rendering of each reading, with its row count and first rows, is checked against the request (Noul per reading, plus a comparative Choice). An alternative replaces the main reading only by a clear margin. A low score is reported as *uncertain*, and the alternatives are offered.

This second Jev pass sees new evidence that the first pass did not: the complete reading in plain English and its actual result. It catches readings that are locally plausible but globally wrong (EXPERIMENTS E7, E10, E11).

## What Jev never does

- write or edit SQL, or name tables, columns or values;
- decide join paths, keys, subquery shapes or set operations;
- see or change data;
- act without a bounded option set (every Choice lists its options; every Noul is a yes/no about given text).
