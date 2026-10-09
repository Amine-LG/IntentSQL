import sqlite3
import tempfile
import unittest
from pathlib import Path

from intentsql.grounding import extract_literals, ground
from intentsql.intent import column_phrase, entity
from intentsql.schema import load_schema
from intentsql.text import humanize, lemma, stem, tokenize
from tests.fakes import make_shop


class TextTest(unittest.TestCase):
    def test_humanize_identifiers(self):
        self.assertEqual(humanize("Song_release_year"), "song release year")
        self.assertEqual(humanize("StuID"), "stu id")
        self.assertEqual(humanize("firstName"), "first name")

    def test_lemma_and_stem(self):
        self.assertEqual(lemma("countries"), "country")
        self.assertEqual(lemma("classes"), "class")
        self.assertEqual(lemma("address"), "address")
        self.assertEqual(lemma("people"), "person")
        self.assertEqual(stem("ordered"), stem("orders"))

    def test_tokenize_keeps_offsets(self):
        tokens = tokenize("Ann's 3.5 cups")
        self.assertEqual([t.text for t in tokens], ["Ann", "3.5", "cups"])   # possessive split off
        self.assertEqual(tokens[1].start, 6)


class LiteralTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.schema = load_schema(make_shop(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def literals(self, question):
        return [(lit.text, lit.value, lit.kind, lit.columns) for lit in extract_literals(question, self.schema)]

    def test_stored_values_link_to_their_columns(self):
        self.assertEqual(self.literals("customers in Paris"),
                         [("Paris", "Paris", "text", ("customers.city",))])

    def test_case_and_plural_variants_match_stored_values(self):
        found = self.literals("orders that are PAID")
        self.assertEqual(found[0][1], "paid")
        found = self.literals("all lattes")
        self.assertEqual(found[0][1], "Latte")

    def test_quoted_text_is_a_literal_even_if_unknown(self):
        self.assertEqual(self.literals('products named "Dark Roast"')[0][:3],
                         ("Dark Roast", "Dark Roast", "text"))

    def test_numbers_number_words_and_ordinals(self):
        values = [(t, v) for t, v, k, _ in self.literals("top three orders over 1,200.5 in the fifth week")]
        self.assertEqual(values, [("three", 3), ("1,200.5", 1200.5), ("fifth", 5)])

    def test_rank_ordinals_are_not_values(self):
        self.assertEqual(self.literals("the second highest salary"), [])

    def test_dates(self):
        self.assertEqual(self.literals("placed before February 1, 2024")[0][1:3], ("2024-02-01", "date"))
        self.assertEqual(self.literals("since 2024-03-01")[0][1:3], ("2024-03-01", "date"))

    def test_letters_and_one_as_pronoun(self):
        self.assertEqual(self.literals("names ending with the letter m")[0][1], "m")
        self.assertEqual(self.literals("the one with the highest price"), [])

    def test_possessives(self):
        g = ground("List each employee's name and salary.", self.schema)
        self.assertEqual(g.table_link["employees"], 1.0)        # "employee's" names the table
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.sqlite"
            with sqlite3.connect(path) as conn:
                conn.executescript("CREATE TABLE shop (id INTEGER PRIMARY KEY, name TEXT);"
                                   "INSERT INTO shop VALUES (1, 'McDonald''s'), (2, 'Wendy');")
            found = extract_literals("Where is McDonald's located?", load_schema(path))
            self.assertEqual([(lit.text, lit.value) for lit in found], [("McDonald's", "McDonald's")])

    def test_grounding_links_and_positions(self):
        g = ground("What is the price of products in the coffee category?", self.schema)
        self.assertGreater(g.column_link["products.price"], 0)
        self.assertLess(g.mention_position["products.price"], g.mention_position["products.category"])


class PhraseTest(unittest.TestCase):
    def test_entity_and_column_phrases(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = load_schema(make_shop(tmp))
            self.assertEqual(entity(schema, "customers"), "customer")
            self.assertEqual(column_phrase(schema, "customers.name"), "customer name")
            self.assertEqual(column_phrase(schema, "order_items.quantity"), "order item quantity")


if __name__ == "__main__":
    unittest.main()
