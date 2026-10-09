"""Derived measures (arithmetic over stored columns) and column-to-column comparisons."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from intentsql.grounding import ground
from intentsql.pipeline import ANSWERED, answer
from intentsql.schema import load_schema
from tests.fakes import ScriptedJev, make_shop

DDL = """
CREATE TABLE candidate (id INTEGER PRIMARY KEY, name TEXT, support_rate REAL, oppose_rate REAL);
CREATE TABLE stay (id INTEGER PRIMARY KEY, guest TEXT, check_in_date DATE, check_out_date DATE);
INSERT INTO candidate VALUES (1,'Ann',0.4,0.3),(2,'Bob',0.2,0.5),(3,'Cy',0.25,0.35);
INSERT INTO stay VALUES (1,'Ann','2024-01-01','2024-01-04'),(2,'Bob','2024-02-01','2024-02-11'),
  (3,'Cy','2024-03-01','2024-03-02');
"""


def rows(result):
    return sorted(tuple(r) for r in result.rows)


class MeasureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.shop = make_shop(cls.tmp.name)
        cls.db = Path(cls.tmp.name) / "m.sqlite"
        with sqlite3.connect(cls.db) as conn:
            conn.executescript(DDL)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ask(self, db, question, rules):
        result = answer(db, question, client=ScriptedJev(rules))
        self.assertEqual(result.status, ANSWERED, result.message)
        return result

    def test_measures_are_proposed_only_when_the_request_calls_for_them(self):
        schema = load_schema(self.shop)
        self.assertFalse([c for c in ground("Which product is the most expensive?", schema).schema.columns
                          if c.formula])
        derived = [c for c in ground("Which product brought in the most revenue?", schema).schema.columns
                   if c.formula]
        self.assertEqual([c.formula for c in derived], [("*", "order_items.quantity", "products.price")])
        self.assertEqual(derived[0].table, "order_items")       # the 'many' side: no repeated rows
        self.assertNotIn("__m", " ".join(load_schema(self.shop).tables["order_items"].columns[-1].name))

    def test_revenue_is_quantity_times_price(self):
        # Mocha: (3 + 1) * 4.0 = 16 beats Scone: (1 + 4) * 3.0 = 15; counting order lines
        # or summing quantities would pick a different product.
        result = self.ask(self.shop, "Which product brought in the most revenue?", [
            ("^out_", "product name in", "itself"),
            ("^order$", "", r"total .*revenue.*highest first"),
            ("^limit$", "", "single top")])
        self.assertEqual(rows(result), [("Mocha",)])
        self.assertIn("*", result.sql)

    def test_column_against_column(self):
        result = self.ask(self.db, "Which candidates have a lower support rate than oppose rate?", [
            ("^out_", "candidate name in", "itself"),
            ("^colcmp$", "", "support rate is less than the candidate oppose rate")])
        self.assertEqual(rows(result), [("Bob",), ("Cy",)])

    def test_duration_between_two_dates(self):
        result = self.ask(self.db, "Which guest had the longest stay?", [
            ("^out_", "stay guest in", "itself"),
            ("^order$", "", r"duration.*highest first"),
            ("^limit$", "", "single top")])
        self.assertEqual(rows(result), [("Bob",)])
        self.assertIn("JULIANDAY", result.sql)


if __name__ == "__main__":
    unittest.main()
