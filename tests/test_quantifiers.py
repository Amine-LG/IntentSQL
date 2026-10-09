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
CREATE TABLE section (id INTEGER PRIMARY KEY, course TEXT, semester TEXT, year INTEGER);
INSERT INTO section VALUES (1,'C1','Fall',2009),(2,'C2','Spring',2009),(3,'C3','Spring',2010),(4,'C4','Fall',2010);
CREATE TABLE allergy (student TEXT, allergen TEXT);
INSERT INTO allergy VALUES ('Al','Milk'),('Al','Cat'),('Ed','Milk'),('Cy','Cat');
CREATE TABLE staff (id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT);
CREATE TABLE problem (id INTEGER PRIMARY KEY, product TEXT, reported_by INTEGER REFERENCES staff(id));
INSERT INTO staff VALUES (1,'Mia','Ross'),(2,'Bo','Kim'),(3,'Mia','Kim');
INSERT INTO problem VALUES (1,'X',1),(2,'X',2),(3,'Y',1),(4,'Z',3),(5,'W',2);
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


    NAME_RULES = [("^out_", "problem product in", "itself"),
                  ("^role_", "", "filtered by"),
                  ("^lop_", "", "exactly this value")]

    def test_full_names_are_one_record_each(self):
        # Either person: Mia Kim (first name of one, last name of the other) must not match.
        result = self.ask("Which products had problems reported by Mia Ross or Bo Kim?", self.NAME_RULES)
        self.assertEqual(rows(result), [("W",), ("X",), ("X",), ("Y",)])

    def test_value_but_not_value(self):
        result = self.ask("Which students are allergic to Milk but not Cat?", [
            ("^out_", "allergy student in", "itself"),
            ("^role_", "", "filtered by"),
            ("^lop_", "", "exactly this value")])
        self.assertEqual(rows(result), [("Ed",)])

    def test_semester_of_year_pairs(self):
        result = self.ask("Which courses ran in the Fall of 2009 or the Spring of 2010?", [
            ("^out_", "section course in", "itself"),
            ("^role_", "", "filtered by|compared"),
            ("^lcol_", r"\[2009\]|\[2010\]", "section year"),
            ("^lop_", r"\[2009\]|\[2010\]", "^equal"),
            ("^lop_", "", "exactly this value")])
        self.assertEqual(rows(result), [("C1",), ("C3",)])

    def test_both_people(self):
        result = self.ask("Which products had problems reported by both Mia Ross and Bo Kim?", self.NAME_RULES)
        self.assertEqual(rows(result), [("X",)])


class FlagTest(unittest.TestCase):
    """Two-valued flags (retail fixture: products.discontinued is 0/1)."""

    DB = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "retail.sqlite"

    def test_no_flagged_related_records_is_an_anti_join(self):
        result = answer(self.DB, "Which suppliers have no discontinued products?", client=ScriptedJev([
            ("^out_", "show the supplier name in", "itself"),
            ("^cat_products.discontinued$", "", "not discontinued")]))
        self.assertEqual(result.status, ANSWERED, result.message)
        with sqlite3.connect(self.DB) as conn:
            gold = conn.execute("SELECT name FROM suppliers WHERE id NOT IN "
                                "(SELECT supplier_id FROM products WHERE discontinued = 1)").fetchall()
        self.assertEqual(rows(result), sorted(gold))

    def test_flag_stated_as_a_predicate(self):
        from intentsql.grounding import ground
        from intentsql.interpret import Option, _stated_flag
        from intentsql.jev import Answer
        from intentsql.schema import load_schema
        g = ground("List products that are discontinued or cost more than 100.", load_schema(self.DB))
        options = [Option("v0", "discontinued", "1"), Option("v1", "not discontinued", "0")]
        hesitant = Answer("choice", "none", {"v0": 0.47, "v1": 0.0, "none": 0.53})
        self.assertEqual(_stated_flag(g, "products.discontinued", options, hesitant).choice, "v0")
        sure = Answer("choice", "none", {"v0": 0.1, "v1": 0.0, "none": 0.9})
        self.assertEqual(_stated_flag(g, "products.discontinued", options, sure).choice, "none")


if __name__ == "__main__":
    unittest.main()
