"""Group-level quantifiers: 'never', 'always', 'both', and aggregates over a subset.

These readings need set operations or filtered subqueries; a row-level filter
returns plausible but wrong rows (the regression each test guards against).
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from intentsql.pipeline import ANSWERED, answer
from tests.fakes import ScriptedJev

DDL = """
CREATE TABLE film (id INTEGER PRIMARY KEY, title TEXT, studio TEXT, director TEXT, minutes INTEGER);
CREATE TABLE weather (day TEXT, zip_code INTEGER, max_dew_point INTEGER);
CREATE TABLE wine (id INTEGER PRIMARY KEY, name TEXT, year INTEGER, price REAL);
INSERT INTO film VALUES (1,'A','Paramount','Walter Hill',100),(2,'B','Paramount','John Ford',170),
  (3,'C','Fox','John Ford',80),(4,'D','Fox','Ann Lee',155),(5,'E','MGM','Walter Hill',120),
  (6,'F','MGM','John Ford',160),(7,'G','Fox','Ann Lee',85);
INSERT INTO weather VALUES ('d1',94107,60),('d2',94107,65),('d3',94301,72),('d4',94301,50),
  ('d5',95113,69);
INSERT INTO wine VALUES (1,'Alpha',2006,30),(2,'Beta',2006,45),(3,'Gamma',2010,50),
  (4,'Delta',2012,20),(5,'Eps',2008,46);
"""


def rows(result):
    return sorted(tuple(r) for r in result.rows)


class QuantifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "q.sqlite"
        with sqlite3.connect(cls.db) as conn:
            conn.executescript(DDL)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ask(self, question, rules):
        result = answer(self.db, question, client=ScriptedJev(rules))
        self.assertEqual(result.status, ANSWERED, result.message)
        return result

    def test_have_not_with_non_identifying_output_is_a_set_difference(self):
        # Paramount and MGM have films by Walter Hill; Fox has none.  A row filter
        # (director != 'Walter Hill') would wrongly return all three studios.
        result = self.ask('Show the studios that have not produced films with director "Walter Hill".', [
            ("^out_", "film studio in", "itself"),
            ("^role_", r"\[\"?Walter Hill", "filtered by"),
            ("^lop_", r"Walter Hill", "must NOT have")])
        self.assertEqual(rows(result), [("Fox",)])
        self.assertIn("EXCEPT", result.sql)

    def test_always_is_a_universal_condition(self):
        result = self.ask("Which zip codes had a max dew point that was always below 70?", [
            ("^out_", "zip code in", "itself"),
            ("^role_", r"\[70\]", "compared"),
            ("^lcol_", r"\[70\]", "max dew point"),
            ("^lop_", r"\[70\]", "^less than"),
            ("^lall_", r"\[70\]", 0.95)])
        self.assertEqual(rows(result), [(94107,), (95113,)])

    def test_aggregate_over_a_described_subset(self):
        # 'above any wine produced in 2006' compares with MAX(price) over 2006 wines (45).
        result = self.ask("Give the names of wines with prices above any wine produced in 2006.", [
            ("^out_", "wine name in", "itself"),
            ("^role_", r"\[2006\]", "compared against"),
            ("^lcol_", r"\[2006\]", "wine year"),
            ("^lop_", r"\[2006\]", "^equal"),
            ("^vsagg$", "", "price is greater than the maximum wine price")])
        self.assertEqual(rows(result), [("Eps",), ("Gamma",)])
        self.assertIn("2006", result.explanation)

    def test_both_values_come_from_different_rows(self):
        result = self.ask("Which studios have films directed by both Walter Hill and John Ford?", [
            ("^out_", "film studio in", "itself"),
            ("^role_", r"\[(Walter Hill|John Ford)\]", "filtered by"),
            ("^lcol_", "", "director"),
            ("^lop_", "", "exactly this value")])
        self.assertEqual(rows(result), [("MGM",), ("Paramount",)])

    def test_disjoint_ranges_on_one_column_describe_different_rows(self):
        result = self.ask("Which studios have films longer than 150 minutes and films shorter "
                          "than 90 minutes?", [
            ("^out_", "film studio in", "itself"),
            ("^role_", r"\[(150|90)\]", "compared"),
            ("^lcol_", "", "minutes"),
            ("^lop_", r"\[150\]", "^greater than"),
            ("^lop_", r"\[90\]", "^less than")])
        self.assertEqual(rows(result), [("Fox",)])
        self.assertIn("INTERSECT", result.sql)


if __name__ == "__main__":
    unittest.main()
