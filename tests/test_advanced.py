"""Nested aggregates, percentages, outer joins, top-N per group and date periods."""

import tempfile
import unittest
from datetime import date

from intentsql.grounding import periods
from intentsql.pipeline import ANSWERED, answer
from tests.fakes import ScriptedJev, make_shop


def rows(result):
    return sorted(tuple(r) for r in result.rows)


class AdvancedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = make_shop(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ask(self, question, rules):
        jev = ScriptedJev(rules)
        result = answer(self.db, question, client=jev)
        self.assertEqual(result.status, ANSWERED, result.message)
        return result

    def test_average_count_per_parent_counts_zero(self):
        # Orders per customer: 2, 1, 1, 0 (Di), 1 -> 1.0; ignoring Di would give 1.25.
        result = self.ask("What is the average number of orders per customer?", [
            ("^count$", "", "average number of order records per customer")])
        self.assertEqual(rows(result), [(1.0,)])
        self.assertIn("LEFT JOIN", result.sql)

    def test_average_order_value_is_not_average_line_value(self):
        # Order totals 10, 12, 2.5, 7.5, 12 -> 8.8; the average line value would be 44 / 7.
        result = self.ask("What is the average order value?", [
            ("^out_", "show the order item value", "per-order total")])
        self.assertAlmostEqual(result.rows[0][0], 8.8)
        self.assertIn("per-order total", result.explanation)

    def test_percentage_asks_which_condition_is_the_share(self):
        result = self.ask("What percentage of orders were cancelled?", [
            ("^count$", "", "how many order records"),
            ("^role_", r"\[cancelled\]", "filtered by"),
            ("^lop_", r"\[cancelled\]", "exactly this value"),
            ("^share$", "", "cancelled")])
        self.assertEqual(rows(result), [(20.0,)])
        self.assertIn("CASE WHEN", result.sql)

    def test_include_groups_without_related_records(self):
        result = self.ask("How many orders does each customer have, including customers with no orders?", [
            ("^count$", "", "how many order records"),
            ("^group$", "", "distinct order customer id$")])
        self.assertEqual(rows(result), [("Ann", 2), ("Bob", 1), ("Cy", 1), ("Di", 0), ("Ed", 1)])

    def test_top_n_within_each_group(self):
        result = self.ask("List the two cheapest products in each category.", [
            ("^out_", "show the product name in", "itself"),
            ("^order$", "", "product price, lowest first"),
            ("^limit$", "", "top 2"),
            ("^role_", r"\[two\]", "how many results"),
            ("^group$", "", "product category$")])
        self.assertEqual(rows(result), [("Green",), ("Latte",), ("Mocha",), ("Scone",)])
        self.assertIn("ROW_NUMBER() OVER (PARTITION BY", result.sql)

    def test_month_period(self):
        result = self.ask("How many orders were placed in February 2024?", [
            ("^count$", "", "how many order records"),
            ("^role_", r"\[February 2024\]", "time period"),
            ("^lop_", "", "during this period")])
        self.assertEqual(rows(result), [(2,)])
        self.assertIn("February 2024", result.explanation)

    def test_period_parsing(self):
        today = date(2026, 10, 9)
        found = {text: value[:2] for _, _, text, value in periods(
            "orders in Q1 2024, last month, the last 30 days and March 2023", today)}
        self.assertEqual(found["Q1 2024"], ("2024-01-01", "2024-04-01"))
        self.assertEqual(found["last month"], ("2026-09-01", "2026-10-01"))
        self.assertEqual(found["the last 30 days"], ("2026-09-09", "2026-10-10"))
        self.assertEqual(found["March 2023"], ("2023-03-01", "2023-04-01"))


if __name__ == "__main__":
    unittest.main()
