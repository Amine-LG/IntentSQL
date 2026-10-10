import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests.fakes import ScriptedJev, make_shop


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INTENTSQL_WORKSPACE": self.tmp.name,
                                                "SYSTEM_ONE_API_KEY": "", "TYPESAFE_API_KEY": "",
                                                "OPENROUTER_API_KEY": "", "OPENJEV_API_KEY": "",
                                                "INTENTSQL_PROVIDER": "", "XDG_CONFIG_HOME": self.tmp.name,
                                                "INTENTSQL_NO_DOTENV": "1"})
        self.env.start()
        from intentsql import config, jev, web
        config.settings.cache_clear()
        jev.reset_shared_client()
        self.web = web
        self.client = TestClient(web.app)
        self.shop = make_shop(self.tmp.name)

    def tearDown(self):
        from intentsql import config, jev
        self.env.stop()
        config.settings.cache_clear()
        jev.reset_shared_client()
        self.tmp.cleanup()

    def upload(self, name="shop.sqlite"):
        return self.client.post(f"/api/databases/upload?name={name}", content=self.shop.read_bytes())

    def test_bundled_databases_listed(self):
        names = {d["name"] for d in self.client.get("/api/databases").json()}
        self.assertTrue({"cyberchase.db", "dese.db", "moneyball.db"} <= names)

    def test_upload_schema_preview_and_delete(self):
        entry = self.upload().json()
        self.assertFalse(entry["bundled"])
        schema = self.client.get(f"/api/databases/{entry['name']}/schema").json()
        self.assertIn("customers", [t["name"] for t in schema["tables"]])
        preview = self.client.get(f"/api/databases/{entry['name']}/tables/customers?limit=2").json()
        self.assertEqual(len(preview["rows"]), 2)
        self.assertEqual(self.client.delete(f"/api/databases/{entry['name']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/databases/{entry['name']}/schema").status_code, 404)

    def test_bundled_cannot_be_deleted_and_names_are_validated(self):
        self.assertEqual(self.client.delete("/api/databases/dese.db").status_code, 400)
        self.assertEqual(self.client.get("/api/databases/..%2Fsecret/schema").status_code, 404)

    def test_non_sqlite_upload_rejected(self):
        response = self.client.post("/api/databases/upload?name=x.db", content=b"not a database")
        self.assertEqual(response.status_code, 400)

    def test_console_is_read_only(self):
        name = self.upload().json()["name"]
        ok = self.client.post("/api/sql", json={"database": name, "sql": "SELECT COUNT(*) FROM orders"})
        self.assertEqual(ok.json()["rows"], [[5]])
        for sql in ("DELETE FROM orders", "DROP TABLE orders", "UPDATE orders SET total = 0",
                    "ATTACH DATABASE 'x.db' AS x", "PRAGMA writable_schema = 1"):
            with self.subTest(sql=sql):
                self.assertEqual(self.client.post("/api/sql", json={"database": name, "sql": sql}).status_code, 400)
        with sqlite3.connect(self.tmp.name + "/databases/" + name) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 5)

    def test_cross_origin_writes_rejected(self):
        response = self.client.post("/api/sql", json={"database": "dese.db", "sql": "SELECT 1"},
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_connection_key_is_saved_privately_and_never_returned(self):
        response = self.client.post("/api/connection", json={"provider": "typesafe", "api_key": "secret-key"})
        data = response.json()
        self.assertEqual((data["provider"], data["model"], data["has_key"]), ("typesafe", "jev-latest", True))
        self.assertNotIn("secret-key", response.text)
        switched = self.client.post("/api/connection", json={"provider": "openrouter",
                                                             "api_key": "other-secret"}).json()
        self.assertEqual((switched["provider"], switched["model"]), ("openrouter", "typesafe/jev-1.13"))
        self.assertEqual({p["name"] for p in switched["providers"] if p["has_key"]},
                         {"typesafe", "openrouter", "mock"})
        self.assertNotIn("other-secret", self.client.get("/api/connection").text)
        self.assertEqual(self.client.get("/api/health").json()["provider"], "openrouter")
        self.assertNotIn("secret-key", self.client.get("/api/connection").text)
        path = Path(self.tmp.name) / "connection.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        bad = self.client.post("/api/connection", json={"url": "http://example.com"})
        self.assertEqual(bad.status_code, 400)

    def test_ask_requires_a_key_then_answers(self):
        name = self.upload().json()["name"]
        missing = self.client.post("/api/ask", json={"database": name, "question": "How many orders?"})
        self.assertEqual(missing.status_code, 400)
        jev = ScriptedJev([("^count$", "", "how many order records")])
        with mock.patch.object(self.web, "shared_client", return_value=jev), \
                mock.patch("intentsql.pipeline.shared_client", return_value=jev):
            result = self.client.post("/api/ask", json={"database": name, "question": "How many orders?"}).json()
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["rows"], [[5]])
        self.assertIn("decisions", result)


    def test_streamed_answer_reports_progress_then_result(self):
        name = self.upload().json()["name"]
        jev = ScriptedJev([("^count$", "", "how many order records")])
        with mock.patch.object(self.web, "shared_client", return_value=jev), \
                mock.patch("intentsql.pipeline.shared_client", return_value=jev):
            response = self.client.post("/api/ask/stream", json={"database": name, "question": "How many orders?"})
        events = [block.split("\n") for block in response.text.strip().split("\n\n")]
        kinds = [lines[0].removeprefix("event: ") for lines in events]
        self.assertIn("phase", kinds)
        self.assertEqual(kinds[-1], "result")
        result = json.loads(events[-1][1].removeprefix("data: "))
        self.assertEqual(result["rows"], [[5]])

    def test_examples_and_parameterized_console(self):
        name = self.upload().json()["name"]
        self.assertIn("How many orders does each customer have?",
                      self.client.get(f"/api/databases/{name}/examples").json())
        rows = self.client.post("/api/sql", json={"database": name, "sql": "SELECT name FROM customers WHERE city = ?",
                                                  "params": ["Lyon"]}).json()["rows"]
        self.assertEqual(sorted(rows), [["Bob"], ["Ed"]])


if __name__ == "__main__":
    unittest.main()
