import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intentsql.benchmark.analysis import diagnose
from intentsql.benchmark.exec_match import (
    execute,
    normalize_rows,
    order_matters,
    results_equivalent,
    set_equivalent,
)
from intentsql.benchmark.runner import ANSWERED, REFUSED, Prediction, evaluate_case, summarize
from intentsql.benchmark.spider import Example, construct_tags, hardness, primary_category, stratified_sample
from tests.fakes import sqlite


class ResultEquivalenceTest(unittest.TestCase):
    def test_multiset_ignores_row_order_but_not_duplicates(self):
        self.assertTrue(results_equivalent([(1,), (2,)], [(2,), (1,)], order_matters=False))
        self.assertFalse(results_equivalent([(1,), (1,)], [(1,), (2,)], order_matters=False))
        self.assertFalse(results_equivalent([(1,), (1,)], [(1,)], order_matters=False))

    def test_order_matters_when_reference_orders(self):
        self.assertFalse(results_equivalent([(1,), (2,)], [(2,), (1,)], order_matters=True))
        self.assertTrue(results_equivalent([(1,), (2,)], [(1,), (2,)], order_matters=True))

    def test_column_permutation_allowed(self):
        self.assertTrue(results_equivalent([(1, "a"), (2, "b")], [("a", 1), ("b", 2)],
                                           order_matters=False))
        self.assertFalse(results_equivalent([(1, "a"), (2, "b")], [("b", 1), ("a", 2)],
                                            order_matters=False))

    def test_floats_and_integers_normalize(self):
        rows = normalize_rows([(2.0,), (1.0000000001,)])
        self.assertEqual(rows, [(2,), (1,)])

    def test_set_equivalence_is_lenient_about_duplicates(self):
        self.assertTrue(set_equivalent([(1,), (1,), (2,)], [(2,), (1,)]))
        self.assertFalse(set_equivalent([(1,)], [(2,)]))

    def test_empty_results_match(self):
        self.assertTrue(results_equivalent([], [], order_matters=False))

    def test_order_detection(self):
        self.assertTrue(order_matters("select a from t ORDER  BY a"))
        self.assertFalse(order_matters("select a from t"))


class SpiderMetadataTest(unittest.TestCase):
    def test_hardness_easy_and_extra(self):
        easy = {"from": {"table_units": [["table_unit", 0]], "conds": []},
                "select": [False, [[3, [0, [0, 0, False], None]]]], "where": [], "groupBy": [],
                "having": [], "orderBy": [], "limit": None, "intersect": None, "union": None,
                "except": None}
        self.assertEqual(hardness(easy), "easy")
        nested = dict(easy, intersect=easy, where=[[False, 2, [0, [0, 1, False], None], 1, None]],
                      orderBy=["asc", [[0, [0, 1, False], None]]], limit=1)
        self.assertEqual(hardness(nested), "extra")

    def test_construct_tags(self):
        tags = construct_tags("SELECT a FROM t JOIN u ON t.x=u.x GROUP BY a HAVING count(*) > 1")
        self.assertIn("join", tags)
        self.assertIn("having", tags)
        self.assertEqual(primary_category(tags), "having")
        self.assertEqual(primary_category(construct_tags(
            "SELECT a FROM t WHERE a IN (SELECT b FROM u)")), "nested")

    def test_stratified_sample_is_seeded_and_covers_strata(self):
        examples = [Example("dev", i, "db", "q", "SELECT 1", h, ("filter",), Path("x"))
                    for i, h in enumerate(["easy"] * 50 + ["medium"] * 30 + ["hard"] * 15
                                          + ["extra"] * 5)]
        first = stratified_sample(examples, 20, 3)
        self.assertEqual([e.index for e in first],
                         [e.index for e in stratified_sample(examples, 20, 3)])
        self.assertEqual(len(first), 20)
        self.assertEqual({e.hardness for e in first}, {"easy", "medium", "hard", "extra"})
        self.assertNotEqual([e.index for e in first],
                            [e.index for e in stratified_sample(examples, 20, 4)])


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.sqlite"
        with sqlite(self.db) as conn:
            conn.executescript("CREATE TABLE t(a INTEGER, b TEXT);"
                               "INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'y');")

    def tearDown(self):
        self.tmp.cleanup()

    def example(self, query):
        return Example("dev", 0, "t", "q", query, "easy", construct_tags(query), self.db)

    def test_equivalent_sql_scores_correct(self):
        case = evaluate_case(self.example("SELECT count(*) FROM t WHERE b = 'y'"),
                             Prediction(ANSWERED, "SELECT COUNT(a) FROM t WHERE b = ?", ("y",)))
        self.assertTrue(case.correct)

    def test_wrong_and_refused_are_classified(self):
        wrong = evaluate_case(self.example("SELECT a FROM t WHERE b = 'y'"),
                              Prediction(ANSWERED, "SELECT a FROM t", ()))
        self.assertFalse(wrong.correct)
        self.assertIn("filter", wrong.errors)
        refused = evaluate_case(self.example("SELECT a FROM t"), Prediction(REFUSED))
        summary = summarize([wrong, refused])
        self.assertEqual(summary["execution_accuracy"], 0)
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["answered_rate"], 0.5)

    def test_sql_error_is_not_executable(self):
        case = evaluate_case(self.example("SELECT a FROM t"),
                             Prediction(ANSWERED, "SELECT nope FROM t", ()))
        self.assertFalse(case.executable)
        self.assertIn("sql_error", case.errors)

    def test_execute_is_read_only(self):
        result = execute(self.db, "DELETE FROM t")
        self.assertIsNotNone(result.error)

    def test_diagnose_ordering(self):
        self.assertIn("ordering", diagnose("SELECT a FROM t ORDER BY a DESC",
                                           "SELECT a FROM t ORDER BY a ASC"))


if __name__ == "__main__":
    unittest.main()


class ProviderFailureTest(unittest.TestCase):
    def test_fatal_provider_errors_stop_the_run_instead_of_scoring_errors(self):
        from intentsql.benchmark.suites import load_suite, run_suite
        from intentsql.benchmark.systems import ProviderStopped, intentsql_system
        from intentsql.jev import JevError
        from intentsql.mock import MockClient

        class Expired(MockClient):
            def ask(self, *args, **kwargs):
                raise JevError("key expired", fatal=True)

        cases = load_suite(Path(__file__).resolve().parents[1] / "benchmarks" / "suites" / "smoke.json")[:2]
        with mock.patch("intentsql.pipeline.shared_client", return_value=Expired()):
            with self.assertRaises(ProviderStopped):
                run_suite(intentsql_system(), cases, workers=1)
