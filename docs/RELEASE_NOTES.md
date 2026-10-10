# IntentSQL 1.0.0-beta.1

A Beta of the rewritten IntentSQL. You ask a SQLite database questions in plain English, and the app shows how each answer was reached: the values it found, each decision with the options it was offered, the plan, the verification, and the read-only SQL it ran.

## What's new since the Alpha (v0.1.0-alpha.1)

- **A new architecture.**
  - Code proposes every option.
  - A decision model only chooses among them, in one parallel round.
  - A typed plan compiles to parameterized, read-only SQL.
  - Alternative readings that give different results are run and checked.
- **Much wider SQL coverage:** multi-hop, self- and outer joins; subqueries; INTERSECT and EXCEPT; anti-joins; nested aggregates; percentages; derived measures; top-N per group; periods. The Alpha handled one join hop and no subqueries.
- **A new web app:**
  - first-run model setup with honest connection states;
  - a visual decision inspector;
  - history, CSV export, light and dark themes, a mobile layout.
- **Open to any compatible model:**
  - TypeSafe Jev and Liquid AI d1 are built in;
  - **custom decision models** (your own System One endpoint) can be added in the app or the CLI, and are tested for compatibility;
  - there is an explicitly labelled demo mode, and never a hidden fallback.
- **Read-only:** requests to change data are refused, and queries run on read-only connections.
- **Engineering:** CI, secret scanning, a dependency audit, a Docker image and a CLI.

## How good is it?

Historical measurement with TypeSafe Jev, on code from 2026-10-09:
- **76.8% ± 2.6** execution accuracy on Spider dev (two random batches of 300);
- 86% of those answers were confident, and **86.0%** of the confident answers were correct;
- **91.7%** correct on the most confident 70% of questions.

The Alpha scored 33.8% on 80 Spider train questions. That figure is not directly comparable: different split, sample size and code. Details are in [docs/EVALUATION.md](EVALUATION.md#results).

This exact build has **not** been measured live: the TypeSafe account had no credits, and no Liquid key was available. Liquid AI d1 support is implemented and tested offline but not yet validated with real answers.

## Known limitations

- SQLite only; one question at a time.
- Extra-hard questions (unions of differently aggregated subqueries, deep nesting) are the weakest area.
- About one confident answer in seven was wrong on Spider dev. Read the "IntentSQL understood" line before trusting a result.
- Confidence is calibrated for TypeSafe Jev only. Custom models are not verified by IntentSQL; a passing connection test checks the format, not decision quality.

## Install

See the [README](../README.md#install): `pip install .` from a clone, or the Docker image. Then choose a model, or try demo mode.
