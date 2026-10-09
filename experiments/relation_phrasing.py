"""Prompt experiment: phrasing of the relationship existence/absence decision.

Each probe is (database, request, subject table, related table, expected answer).
Run: python experiments/relation_phrasing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intentsql.benchmark.spider import spider_root
from intentsql.jev import JevClient, Usage, choice
from intentsql.schema import load_schema

PROBES = [
    ("tracking_orders", "Show all product names without an order.", "Products", "Orders", "lacks"),
    ("tracking_orders", "Show the names of products that have been ordered.", "Products", "Orders", "has"),
    ("tracking_orders", "Show all product names.", "Products", "Orders", "none"),
    ("concert_singer", "What are the names of the stadiums without any concerts?", "stadium", "concert", "lacks"),
    ("concert_singer", "List the names of stadiums that hosted a concert.", "stadium", "concert", "has"),
    ("concert_singer", "List the stadium names and capacities.", "stadium", "concert", "none"),
    ("concert_singer", "Which singers never performed in a concert?", "singer", "concert", "lacks"),
    ("musical", "List the name of musicals that do not have actors.", "musical", "actor", "lacks"),
    ("musical", "Show names of musicals which have at least three actors.", "musical", "actor", "none"),
    ("soccer_2", "What is the total number of students enrolled in schools without any goalies?", "College", "Tryout", "lacks"),
    ("loan_1", "Find the average credit score of the customers who have some loan.", "customer", "loan", "has"),
    ("loan_1", "Find the names of customers who have no loan.", "customer", "loan", "lacks"),
    ("loan_1", "What is the average credit score of all customers?", "customer", "loan", "none"),
    ("cre_Doc_Tracking_DB", "Find the names of documents without any images.", "All_Documents", "Document_Locations", "none"),
    ("customers_and_invoices", "How many customers have no accounts?", "Customers", "Accounts", "lacks"),
    ("customers_and_invoices", "Show names of customers with an account.", "Customers", "Accounts", "has"),
]


def variants(s: str, t: str):
    yield "v1", choice(f"Does `request` restrict {s} records by whether they have related {t} records?",
                       {"none": "no such restriction",
                        "has": f"only {s} records that have at least one related {t} record",
                        "lacks": f"only {s} records that have no related {t} record at all"})
    yield "v2", choice(f"Which {s} does `request` ask about, with respect to {t}?",
                       {"none": f"it does not care whether a {s} has any {t}",
                        "has": f"only {s} that have at least one {t}",
                        "lacks": f"only {s} that have no {t} at all (e.g. 'without any {t}', 'never')"})
    yield "v3", choice({"subject": s, "related": t,
                        "question": "Does `request` keep only `subject` items that have some `related` "
                                    "item, only those that have none, or does it not say?"},
                       {"has": f"keep {s} that have at least one {t}",
                        "lacks": f"keep {s} that have no {t}",
                        "none": f"the request says nothing about {s} having or lacking {t}"})


def main() -> None:
    client = JevClient(cache_dir="")
    root = spider_root()
    scores: dict[str, int] = {}
    for db, request, subject, related, expected in PROBES:
        schema = load_schema(root / "database" / db / f"{db}.sqlite")
        s, t = schema.table(subject).label, schema.table(related).label
        state = {"request": request,
                 "database": {tb.label: [c.label for c in tb.columns] for tb in schema.tables.values()}}
        qs = dict(variants(s, t))
        answers = client.ask(state, qs, usage=Usage())
        row = []
        for name, answer in answers.items():
            ok = answer.choice == expected
            scores[name] = scores.get(name, 0) + ok
            row.append(f"{name}:{answer.choice}{'' if ok else '✗'}")
        print(f"{expected:6} {request[:60]:60} {' '.join(row)}")
    print({k: f"{v}/{len(PROBES)}" for k, v in scores.items()})


if __name__ == "__main__":
    main()
