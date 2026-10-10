"""End-to-end interpretation scenarios with scripted Jev decisions.

Each test scripts only the semantic decisions a correct reading needs and
checks the executed rows, so the deterministic parts (grounding, candidate
generation, assembly, join planning, subquery shapes, compilation) are tested
offline exactly as they run in production.
"""

import tempfile
import unittest

from intentsql.pipeline import AMBIGUOUS, ANSWERED, REFUSED, answer
from tests.fakes import ScriptedJev, make_shop, sqlite

SHOW = "itself"          # output form option: "the X itself (one value per result row)"


def rows(result):
    return sorted(tuple(r) for r in result.rows)


class ScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = make_shop(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ask(self, question, rules):
        result = answer(self.db, question, client=ScriptedJev(rules))
        self.assertEqual(result.status, ANSWERED, result.message)
        return result

    def test_simple_filter(self):
        result = self.ask("What are the names of customers from Paris?", [
            ("^out_", "customer name in", SHOW),
            ("^role_", r"\[Paris\]", "filtered by"),
            ("^lop_", r"\[Paris\]", "exactly this value")])
        self.assertEqual(rows(result), [("Ann",), ("Cy",)])
        self.assertIn("?", result.sql)                        # values are bound, never inlined
        self.assertEqual(result.params, ("Paris",))

    def test_count_with_filter(self):
        result = self.ask("How many orders are paid?", [
            ("^count$", "", "how many order records"),
            ("^role_", r"\[paid\]", "filtered by"),
            ("^lop_", r"\[paid\]", "exactly this value")])
        self.assertEqual(rows(result), [(3,)])

    def test_count_per_group_across_join_shows_group_label(self):
        result = self.ask("How many orders are there in each city?", [
            ("^count$", "", "how many order records"),
            ("^group$", "", "customer city")])
        self.assertEqual(rows(result), [("Lyon", 2), ("Paris", 3)])

    def test_customers_who_never_ordered(self):
        result = self.ask("Which customers never placed an order?", [
            ("^out_", "customer name in", SHOW),
            ("^rel_", "customer records by whether they have related order records", "no related")])
        self.assertEqual(rows(result), [("Di",)])
        self.assertIn("NOT IN", result.sql)

    def test_negated_related_value_is_an_anti_join(self):
        # 'never ordered a Latte' must exclude Ann and Cy entirely, not keep them for
        # their other (non-Latte) order lines.
        result = self.ask("Which customers never ordered a Latte?", [
            ("^out_", "customer name in", SHOW),
            ("^role_", r"\[Latte\]", "filtered by"),
            ("^lop_", r"\[Latte\]", "NOT have")])
        self.assertEqual(rows(result), [("Bob",), ("Di",), ("Ed",)])

    def test_superlative(self):
        result = self.ask("Which product is the most expensive?", [
            ("^out_", "product name in", SHOW),
            ("^order$", "", "product price, highest first"),
            ("^limit$", "", "single top")])
        self.assertEqual(rows(result), [("Mocha",)])

    def test_having_count_threshold(self):
        result = self.ask("Which customers have at least 2 orders?", [
            ("^out_", "customer name in", SHOW),
            ("^role_", r"\[2\]", "how many related records"),
            ("^lcnt_", r"\[2\]", "how many order records"),
            ("^lop_", r"\[2\]", "at least")])
        self.assertEqual(rows(result), [("Ann",)])

    def test_above_average(self):
        result = self.ask("Which employees earn more than the average salary?", [
            ("^out_", "employee name in", SHOW),
            ("^vsagg$", "", "salary is greater than the average")])
        self.assertEqual(rows(result), [("Wim",), ("Xia",)])

    def test_same_attribute_as_a_named_record(self):
        result = self.ask("Which employees work in the same department as Wim?", [
            ("^out_", "employee name in", SHOW),
            ("^role_", r"\[Wim\]", "identifies one particular record"),
            ("^lshr_", r"\[Wim\]", "same employee dept"),
            ("^lop_", r"\[Wim\]", "exactly this value")])
        self.assertEqual(rows(result), [("Val",), ("Wim",), ("Xia",)])

    def test_both_values_across_related_rows_is_intersect(self):
        result = self.ask("Which customers ordered both Latte and Mocha?", [
            ("^out_", "customer name in", SHOW),
            ("^role_", r"\[(Latte|Mocha)\]", "filtered by"),
            ("^lop_", r"\[(Latte|Mocha)\]", "exactly this value"),
            ("^connector$", "", "all conditions")])
        self.assertEqual(rows(result), [("Ann",), ("Cy",)])

    def test_count_does_not_double_count_through_joins(self):
        result = self.ask("How many customers ordered coffee?", [
            ("^count$", "", "how many customer records"),
            ("^role_", r"\[coffee\]", "filtered by"),
            ("^lop_", r"\[coffee\]", "exactly this value")])
        self.assertEqual(rows(result), [(2,)])

    def test_between_range(self):
        result = self.ask("List the ids of orders with a total between 6 and 15.", [
            ("^out_", "order id in", SHOW),
            ("^role_", r"\[(6|15)\]", "compared"),
            ("^lcol_", r"\[(6|15)\]", "order total"),
            ("^lop_", r"\[6\]", "at least"),
            ("^lop_", r"\[15\]", "at most")])
        self.assertEqual(rows(result), [(1,), (4,), (5,)])
        self.assertIn("BETWEEN", result.sql)

    def test_distinct_values(self):
        result = self.ask("What are the different product categories?", [
            ("^out_", "product category in", SHOW)])
        self.assertEqual(rows(result), [("coffee",), ("food",), ("tea",)])

    def test_grouped_average_ordered(self):
        result = self.ask("What is the average salary per department, highest first?", [
            ("^out_", "employee salary in", "average"),
            ("^group$", "", "employee dept"),
            ("^order$", "", "average employee salary per group, highest first")])
        self.assertEqual([tuple(r) for r in result.rows], [("tech", 5500.0), ("sales", 4500.0)])

    def test_natural_date(self):
        result = self.ask("Which orders were placed before February 1, 2024? Give their ids.", [
            ("^out_", "order id in", SHOW),
            ("^role_", r"February", "filtered by"),
            ("^lop_", r"February", "before this date")])
        self.assertEqual(rows(result), [(1,)])

    def test_empty_request_is_refused(self):
        result = answer(self.db, "   ", client=ScriptedJev())
        self.assertEqual(result.status, REFUSED)

    def test_nothing_selected_asks_a_follow_up(self):
        jev = ScriptedJev([("^main_output$", "", "^the customer name itself")])
        result = answer(self.db, "customers please", client=jev)
        self.assertEqual(result.status, ANSWERED)
        self.assertEqual(len(jev.asked), 3)           # round one, a targeted follow-up, verification
        self.assertEqual(len(result.rows), 5)


    def test_writes_and_out_of_scope_requests_are_refused(self):
        jev = ScriptedJev([("^scope$", "", "=modify")])
        result = answer(self.db, "Delete all cancelled orders.", client=jev)
        self.assertEqual(result.status, REFUSED)
        self.assertIn("only reads", result.message)
        jev = ScriptedJev([("^scope$", "", "=other")])
        result = answer(self.db, "What will sales be next year?", client=jev)
        self.assertEqual(result.status, REFUSED)

    def test_missing_values(self):
        with sqlite(self.db) as conn:
            conn.execute("INSERT INTO customers VALUES (6, 'Fay', NULL, 'N')")
        try:
            from intentsql.schema import _CACHE
            _CACHE.clear()
            result = self.ask("Which customers have no city recorded?", [
                ("^out_", "customer name in", SHOW),
                ("^nulls$", "", "customer city is missing")])
            self.assertEqual(rows(result), [("Fay",)])
            self.assertIn("IS NULL", result.sql)
        finally:
            with sqlite(self.db) as conn:
                conn.execute("DELETE FROM customers WHERE id = 6")
            _CACHE.clear()

    def test_above_own_group_average_is_correlated(self):
        result = self.ask("Which employees earn more than the average salary of their department?", [
            ("^out_", "employee name in", SHOW),
            ("^vsagg$", "", "average employee salary of records with the same employee dept")])
        # sales avg 4500 -> Zoe; tech avg 5500 -> Xia, Wim
        self.assertEqual(rows(result), [("Wim",), ("Xia",), ("Zoe",)])

    def test_low_verification_is_reported_as_uncertain(self):
        jev = ScriptedJev([("^out_", "customer name in", SHOW), ("^verify", "", 0.05)])
        result = answer(self.db, "What are the customer names?", client=jev)
        self.assertEqual(result.status, AMBIGUOUS)
        self.assertLess(result.verification, 0.2)
        self.assertIsNone(result.confidence)          # the mock provider has no calibration table
        self.assertEqual(len(result.rows), 5)          # the best reading is still shown

    def test_calibration_is_per_provider(self):
        from intentsql.providers import PROVIDERS
        jev = ScriptedJev([("^out_", "customer name in", SHOW), ("^verify", "", 0.05)])
        jev.provider = PROVIDERS["typesafe"]
        calibrated_result = answer(self.db, "What are the customer names?", client=jev)
        self.assertLess(calibrated_result.confidence, 0.5)      # more likely wrong than right
        jev.provider = PROVIDERS["liquid"]                       # another model: not calibrated
        self.assertIsNone(answer(self.db, "What are the customer names?", client=jev).confidence)

    def test_calibration_is_monotone_and_penalizes_alternatives(self):
        from intentsql.pipeline import calibrated
        scores = [calibrated(p / 20, 0) for p in range(21)]
        self.assertEqual(scores, sorted(scores))
        self.assertLess(calibrated(0.8, 2), calibrated(0.8, 0))

    def test_clearly_better_alternative_replaces_the_main_reading(self):
        class Narrow(ScriptedJev):
            def answer(self, key, question):
                from intentsql.jev import Answer
                if key.startswith("role_"):
                    opts = list(question.options)
                    probs = {k: 0.0 for k in opts}
                    probs["value"], probs["other"] = 0.55, 0.45
                    return Answer("choice", "value", probs, 0.1)
                if key == "pick":
                    return self._choice(question.options, "B")
                return super().answer(key, question)

        jev = Narrow([("^count$", "", "how many order records")])
        result = answer(self.db, "How many orders are paid?", client=jev)
        self.assertEqual(result.status, ANSWERED)
        self.assertEqual(rows(result), [(5,)])          # alternative B: no status filter
        self.assertEqual(len(result.alternatives), 1)

    def test_inspector_decisions_carry_the_question_as_asked(self):
        result = self.ask("How many paid orders are there in each city?", [
            ("^count$", "", "how many order records"), ("^group$", "", "customer city"),
            ("^role_", r"\[paid\]", "filtered by"), ("^lop_", r"\[paid\]", "exactly this value")])
        decisions = {d["key"]: d for d in result.to_dict(debug=True)["decisions"]}
        group = decisions["group"]
        self.assertIn("your question", group["asked"])          # not the chosen option's text
        self.assertNotIn("`request`", group["asked"])
        chosen = max(group["probabilities"], key=group["probabilities"].get)
        self.assertIn("customer city", group["options"][chosen])  # option texts, not bare keys
        self.assertIn("[paid]", decisions["role_L1"]["context"])


if __name__ == "__main__":
    unittest.main()
