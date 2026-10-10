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


    def test_elapsed_time_to_a_stated_reference_date(self):
        # Ann stayed from 2024-01-01: 365 days by the end of 2024 (2024-12-31).
        result = self.ask(self.db, "How many days had each guest stayed since check in by the end of 2024?", [
            ("^out_", "show the stay guest in", "itself"),
            ("^out_", "days since check in date", "itself"),
            ("^role_", r"\[2024\]", "filtered by|compared")])
        self.assertIn("2024-12-31", result.sql if "?" not in result.sql else result.display_sql)
        self.assertEqual(sorted(result.rows)[0], ("Ann", 365.0))
        self.assertNotIn("STRFTIME", result.sql)            # 2024 is the reference, not a filter


    def test_parent_amount_minus_total_of_related_amounts(self):
        # Department budgets minus the total salaries of their employees (retail fixture).
        db = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "retail.sqlite"
        result = self.ask(db, "How much budget does each department have left after paying salaries?", [
            ("^out_", "show the department name in", "itself"),
            ("^out_", "budget left after the total", "budget left")])
        with sqlite3.connect(db) as conn:
            gold = conn.execute("SELECT T2.name, T2.budget - SUM(T1.salary) FROM employees T1 JOIN departments T2 "
                                "ON T1.department_id = T2.id GROUP BY T2.id").fetchall()
        as_sets = lambda rows: sorted(tuple(sorted(map(str, r))) for r in rows)  # noqa: E731 (column order free)
        self.assertEqual(as_sets(result.rows), as_sets(gold))


class SelfReferenceTest(unittest.TestCase):
    """employees.manager_id (undeclared) is a self-reference: the manager is a role table."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.shop = make_shop(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_role_table_is_inferred(self):
        schema = load_schema(self.shop)
        roles = [t for t in schema.tables.values() if t.role_of]
        self.assertEqual([(t.role_of, t.label) for t in roles], [("employees", "manager employees")])
        self.assertNotIn(roles[0].name, [t["name"] for t in schema.describe()["tables"]])

    def test_compare_with_manager(self):
        result = answer(self.shop, "Which employees earn less than their manager?", client=ScriptedJev([
            ("^out_", "show the employee name in", "itself"),
            ("^colcmp$", "", "^the employee salary is less than the manager employee salary")]))
        self.assertEqual(result.status, ANSWERED, result.message)
        self.assertEqual(rows(result), [("Val",), ("Wim",), ("Yan",)])
        self.assertEqual(result.sql.count("FROM employees AS") + result.sql.count("JOIN employees AS"), 2)

    def test_show_manager_name(self):
        result = answer(self.shop, "List each employee's name with the name of their manager.",
                        client=ScriptedJev([("^out_", "show the (manager )?employee name in", "itself")]))
        self.assertEqual(result.status, ANSWERED, result.message)
        self.assertEqual(rows(result), [("Val", "Xia"), ("Wim", "Xia"), ("Yan", "Zoe")])


if __name__ == "__main__":
    unittest.main()
