"""Schema inference: subtypes, fallback joins, row identity."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from intentsql.pipeline import ANSWERED, answer
from intentsql.schema import load_schema
from tests.fakes import ScriptedJev

DDL = """
CREATE TABLE people (people_id INTEGER PRIMARY KEY, name TEXT, hometown TEXT);
CREATE TABLE gymnast (gymnast_id INTEGER PRIMARY KEY REFERENCES people(people_id), total_points REAL);
CREATE TABLE list (last_name TEXT, first_name TEXT, grade INTEGER, classroom INTEGER);
CREATE TABLE teachers (last_name TEXT, first_name TEXT, classroom INTEGER);
CREATE TABLE editor (editor_id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
INSERT INTO people VALUES (1,'Ann','Lyon'),(2,'Bob','Nice'),(3,'Cy','Lyon');
INSERT INTO gymnast VALUES (1, 9.5),(3, 8.0);
INSERT INTO list VALUES ('Kim','Al',1,101),('Lee','Bo',5,102),('Roe','Cy',5,102);
INSERT INTO teachers VALUES ('Kim','Mia',101),('Park','Ned',102);
INSERT INTO editor VALUES (1,'A',30),(2,'B',41),(3,'C',52);
"""


class SchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "s.sqlite"
        with sqlite3.connect(cls.db) as conn:
            conn.executescript(DDL)
        cls.schema = load_schema(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_subtype(self):
        self.assertTrue(self.schema.is_subtype("gymnast", "people"))
        self.assertFalse(self.schema.is_subtype("people", "gymnast"))

    def test_naming_a_subtype_restricts_to_its_rows(self):
        result = answer(self.db, "What are the names of gymnasts?", client=ScriptedJev([
            ("^out_", "show the person name in", "itself")]))
        self.assertEqual(result.status, ANSWERED, result.message)
        self.assertEqual(sorted(result.rows), [("Ann",), ("Cy",)])

    def test_fallback_join_uses_matching_values_not_matching_names(self):
        # list.last_name and teachers.last_name share a name; classroom values match.
        (edge,) = self.schema.soft_edges
        self.assertEqual((edge.columns, edge.ref_columns), (("classroom",), ("classroom",)))

    def test_small_tables_do_not_make_every_column_identifying(self):
        self.assertTrue(self.schema.unique_like("editor.editor_id"))
        self.assertTrue(self.schema.unique_like("editor.name"))
        self.assertFalse(self.schema.unique_like("editor.age"))     # 3 distinct in 3 rows proves nothing


if __name__ == "__main__":
    unittest.main()
