import tempfile
import unittest
from pathlib import Path

from intentsql import ir
from intentsql.compiler import CompileError, compile_query
from intentsql.schema import load_schema
from tests.fakes import sqlite

DDL = """
CREATE TABLE dept(id INTEGER PRIMARY KEY, name TEXT, "group" TEXT);
CREATE TABLE emp(id INTEGER PRIMARY KEY, name TEXT, salary REAL, dept_id INTEGER
                 REFERENCES dept(id), year TEXT, "Full Name" TEXT);
INSERT INTO dept VALUES (1,'R&D','a'),(2,'Sales','b');
INSERT INTO emp VALUES (1,'Ann',100,1,'2014','Ann A'),(2,'Bob',200,1,'2015','Bob B'),
                       (3,'Cy',300,2,'2014','Cy C'),(4,'Di',NULL,NULL,'2016','Di D');
"""


class CompilerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "c.sqlite"
        with sqlite(cls.db) as conn:
            conn.executescript(DDL)
        cls.schema = load_schema(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_query(self, query):
        compiled = compile_query(query, self.schema)
        with sqlite(self.db) as conn:
            return compiled, conn.execute(compiled.sql, compiled.params).fetchall()

    def emp(self, **kw):
        return ir.Select(from_=ir.From(ir.TableRef("emp", "e")), **kw)

    def test_values_are_parameters_and_display_is_rendered(self):
        compiled, rows = self.run_query(self.emp(
            items=(ir.SelectItem(ir.Col("e", "name")),),
            where=ir.Compare(ir.Col("e", "name"), "=", ir.Lit("O'Brien"))))
        self.assertEqual(compiled.sql, "SELECT name FROM emp WHERE name = ?")
        self.assertEqual(compiled.params, ("O'Brien",))
        self.assertIn("'O''Brien'", compiled.display_sql)
        self.assertEqual(rows, [])

    def test_literal_coerced_to_column_storage(self):
        # "year" stores text; an integer literal must compare as text.
        _, rows = self.run_query(self.emp(
            items=(ir.SelectItem(ir.Agg("COUNT", ir.Star())),),
            where=ir.Compare(ir.Col("e", "year"), "=", ir.Lit(2014))))
        self.assertEqual(rows, [(2,)])
        _, rows = self.run_query(self.emp(
            items=(ir.SelectItem(ir.Col("e", "name")),),
            where=ir.Compare(ir.Col("e", "salary"), ">", ir.Lit("150"))))
        self.assertEqual(sorted(rows), [("Bob",), ("Cy",)])

    def test_identifiers_are_quoted_when_needed(self):
        compiled, _ = self.run_query(self.emp(items=(ir.SelectItem(ir.Col("e", "Full Name")),)))
        self.assertIn('"Full Name"', compiled.sql)
        compiled, _ = self.run_query(ir.Select(
            items=(ir.SelectItem(ir.Col("d", "group")),), from_=ir.From(ir.TableRef("dept", "d"))))
        self.assertIn('"group"', compiled.sql)

    def test_join_group_having_order(self):
        query = ir.Select(
            items=(ir.SelectItem(ir.Col("d", "name")), ir.SelectItem(ir.Agg("COUNT", ir.Star()))),
            from_=ir.From(ir.TableRef("emp", "e"), (ir.Join(
                ir.TableRef("dept", "d"), ((ir.Col("e", "dept_id"), ir.Col("d", "id")),)),)),
            group_by=(ir.Col("d", "id"),),
            having=ir.Compare(ir.Agg("COUNT", ir.Star()), ">=", ir.Lit(1)),
            order_by=(ir.OrderItem(ir.Agg("COUNT", ir.Star()), True),), limit=1)
        compiled, rows = self.run_query(query)
        self.assertIn("JOIN dept AS T2 ON T1.dept_id = T2.id", compiled.sql)
        self.assertEqual(rows, [("R&D", 2)])

    def test_not_in_subquery_and_scalar_subquery(self):
        never = ir.Select(items=(ir.SelectItem(ir.Col("d", "name")),),
                          from_=ir.From(ir.TableRef("dept", "d")),
                          where=ir.InQuery(ir.Col("d", "id"), ir.Select(
                              items=(ir.SelectItem(ir.Col("e", "dept_id")),),
                              from_=ir.From(ir.TableRef("emp", "e")),
                              where=ir.Compare(ir.Col("e", "salary"), ">", ir.Lit(250))), True))
        _, rows = self.run_query(never)
        self.assertEqual(rows, [("R&D",)])
        above = self.emp(items=(ir.SelectItem(ir.Col("e", "name")),),
                         where=ir.Compare(ir.Col("e", "salary"), ">", ir.ScalarQuery(ir.Select(
                             items=(ir.SelectItem(ir.Agg("AVG", ir.Col("x", "salary"))),),
                             from_=ir.From(ir.TableRef("emp", "x"))))))
        _, rows = self.run_query(above)
        self.assertEqual(rows, [("Cy",)])

    def test_correlated_subquery(self):
        query = self.emp(items=(ir.SelectItem(ir.Col("e", "name")),),
                         where=ir.Compare(ir.Col("e", "salary"), "=", ir.ScalarQuery(ir.Select(
                             items=(ir.SelectItem(ir.Agg("MAX", ir.Col("x", "salary"))),),
                             from_=ir.From(ir.TableRef("emp", "x")),
                             where=ir.Compare(ir.Col("x", "dept_id"), "=", ir.Col("e", "dept_id"))))))
        _, rows = self.run_query(query)
        self.assertEqual(sorted(rows), [("Bob",), ("Cy",)])

    def test_set_operation(self):
        def names(year):
            return self.emp(items=(ir.SelectItem(ir.Col("e", "name")),),
                            where=ir.Compare(ir.Col("e", "year"), "=", ir.Lit(year)))
        a, b = names("2014"), names("2015")
        b = ir.Select(items=(ir.SelectItem(ir.Col("f", "name")),),
                      from_=ir.From(ir.TableRef("emp", "f")),
                      where=ir.Compare(ir.Col("f", "year"), "=", ir.Lit("2015")))
        _, rows = self.run_query(ir.SetOp("UNION", a, b))
        self.assertEqual(sorted(rows), [("Ann",), ("Bob",), ("Cy",)])

    def test_division_is_real(self):
        _, rows = self.run_query(self.emp(
            items=(ir.SelectItem(ir.BinOp("/", ir.Col("e", "id"), ir.Lit(2))),),
            where=ir.Compare(ir.Col("e", "id"), "=", ir.Lit(1))))
        self.assertEqual(rows, [(0.5,)])

    def test_null_like_between_in_case(self):
        _, rows = self.run_query(self.emp(items=(ir.SelectItem(ir.Col("e", "name")),),
                                          where=ir.IsNull(ir.Col("e", "salary"))))
        self.assertEqual(rows, [("Di",)])
        _, rows = self.run_query(self.emp(items=(ir.SelectItem(ir.Col("e", "name")),),
                                          where=ir.Like(ir.Col("e", "name"), "%o%")))
        self.assertEqual(rows, [("Bob",)])
        _, rows = self.run_query(self.emp(
            items=(ir.SelectItem(ir.Case(((ir.Between(ir.Col("e", "salary"), ir.Lit(100), ir.Lit(200)),
                                           ir.Lit("mid")),), ir.Lit("other"))),),
            where=ir.InList(ir.Col("e", "id"), (ir.Lit(1), ir.Lit(3)))))
        self.assertEqual(sorted(rows), [("mid",), ("other",)])

    def test_rejects_unknown_identifiers_and_bad_structure(self):
        cases = [
            (self.emp(items=(ir.SelectItem(ir.Col("e", "nope")),)), "unknown_column"),
            (self.emp(items=(ir.SelectItem(ir.Col("z", "name")),)), "unknown_alias"),
            (ir.Select(items=(ir.SelectItem(ir.Star()),), from_=ir.From(ir.TableRef("x", "x"))),
             "unknown_table"),
            (self.emp(items=(ir.SelectItem(ir.Col("e", "name")),),
                      where=ir.Compare(ir.Agg("COUNT", ir.Star()), ">", ir.Lit(1))),
             "aggregate_in_where"),
            (self.emp(items=(ir.SelectItem(ir.Agg("SUM", ir.Agg("MAX", ir.Col("e", "id")))),)),
             "nested_aggregate"),
            (self.emp(items=(ir.SelectItem(ir.Func("system", (ir.Lit(1),))),)), "bad_function"),
            (self.emp(items=(ir.SelectItem(ir.Col("e", "name")),), limit=-1), "bad_limit"),
            (ir.SetOp("UNION", self.emp(items=(ir.SelectItem(ir.Col("e", "name")),)),
                      ir.Select(items=(ir.SelectItem(ir.Col("f", "name")),
                                       ir.SelectItem(ir.Col("f", "id"))),
                                from_=ir.From(ir.TableRef("emp", "f")))), "set_width_mismatch"),
        ]
        for query, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(CompileError) as ctx:
                    compile_query(query, self.schema)
                self.assertIn(code, [issue.code for issue in ctx.exception.issues])

    def test_plan_serializes(self):
        plan = ir.to_dict(self.emp(items=(ir.SelectItem(ir.Col("e", "name")),), limit=3))
        self.assertEqual(plan["node"], "Select")
        self.assertEqual(plan["limit"], 3)


    def test_derived_table_and_window(self):
        inner = ir.Select(items=(ir.SelectItem(ir.Col("e", "name"), "name"),
                                 ir.SelectItem(ir.Window("ROW_NUMBER", (ir.Col("e", "dept_id"),),
                                                         (ir.OrderItem(ir.Col("e", "salary"), True),)), "rank")),
                          from_=ir.From(ir.TableRef("emp", "e")), where=ir.IsNull(ir.Col("e", "dept_id"), True))
        outer = ir.Select(items=(ir.SelectItem(ir.Col("d", "name")),),
                          from_=ir.From(ir.Derived(inner, "d")),
                          where=ir.Compare(ir.Col("d", "rank"), "<=", ir.Lit(1)))
        compiled, rows = self.run_query(outer)
        self.assertIn("ROW_NUMBER() OVER (PARTITION BY", compiled.sql)
        self.assertEqual(sorted(rows), [("Bob",), ("Cy",)])

    def test_derived_columns_are_validated(self):
        inner = ir.Select(items=(ir.SelectItem(ir.Agg("SUM", ir.Col("e", "salary")), "total"),),
                          from_=ir.From(ir.TableRef("emp", "e")), group_by=(ir.Col("e", "dept_id"),))
        outer = ir.Select(items=(ir.SelectItem(ir.Agg("AVG", ir.Col("d", "missing"))),),
                          from_=ir.From(ir.Derived(inner, "d")))
        with self.assertRaises(CompileError) as caught:
            compile_query(outer, self.schema)
        self.assertEqual(caught.exception.issues[0].code, "unknown_column")
        ok = ir.Select(items=(ir.SelectItem(ir.Agg("AVG", ir.Col("d", "total"))),),
                       from_=ir.From(ir.Derived(inner, "d")))
        self.assertEqual(self.run_query(ok)[1], [(300.0,)])


class SchemaTest(unittest.TestCase):
    def test_introspection_types_keys_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "s.sqlite"
            with sqlite(db) as conn:
                conn.executescript("""
                    CREATE TABLE a(a_id INTEGER PRIMARY KEY, label TEXT, born DATE);
                    CREATE TABLE b(b_id INTEGER PRIMARY KEY, title TEXT, score TEXT);
                    CREATE TABLE ab(a_id INTEGER REFERENCES a(a_id), b_id INTEGER REFERENCES b(b_id),
                                    PRIMARY KEY(a_id, b_id));
                    CREATE TABLE c(c_id INTEGER PRIMARY KEY, a_id INTEGER);
                    INSERT INTO a VALUES (1,'x','2020-01-02'); INSERT INTO b VALUES (1,'t','3.5');
                """)
            schema = load_schema(db)
            self.assertEqual(schema.column("a.born").kind, "date")
            self.assertEqual(schema.column("b.score").kind, "number")
            self.assertTrue(schema.is_bridge("ab"))
            steps = schema.join_tree(["a", "b"])
            self.assertEqual([s.table for s in steps], ["ab", "b"])
            inferred = [fk for fk in schema.foreign_keys if not fk.declared]
            self.assertEqual([(fk.table, fk.ref_table) for fk in inferred], [("c", "a")])
            self.assertEqual(schema.table("A").name, "a")


if __name__ == "__main__":
    unittest.main()
