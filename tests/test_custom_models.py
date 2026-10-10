"""Custom decision models: user-configured System One endpoints.

The endpoint is simulated with an httpx mock transport: no network, no real key.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from intentsql.jev import Question
from intentsql.mock import MockClient
from tests.fakes import make_shop

REAL_CLIENT = httpx.Client
KEY = "lab-secret-key-123"


def system_one(request):
    """A compatible endpoint, answering with the demo client's fixed defaults."""
    body = json.loads(request.content)
    defaults = MockClient()
    answers = {}
    for key, q in body["questions"].items():
        question = Question(q["type"], q["instructions"], q.get("criteria") if q["type"] == "choice" else None,
                            levels=tuple(q.get("criteria") or ()) if q["type"] == "score" else ())
        a = defaults.answer(key, question)
        answers[key] = ({"noul": a.p} if a.kind == "noul" else
                        {"score": a.score, "probabilities": a.probabilities} if a.kind == "score" else
                        {"choice": a.choice, "probabilities": a.probabilities, "confidence": a.confidence})
    return httpx.Response(200, json={"model": body["model"], "answers": answers, "usage": {"input_tokens": 5}})


def chat_completion(request):
    """An incompatible endpoint (a chat API)."""
    return httpx.Response(200, json={"id": "x", "choices": [{"message": {"content": "billing"}}]})


class CustomModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INTENTSQL_WORKSPACE": self.tmp.name,
                                                "SYSTEM_ONE_API_KEY": "", "TYPESAFE_API_KEY": "",
                                                "LIQUID_API_KEY": "", "INTENTSQL_PROVIDER": "",
                                                "XDG_CONFIG_HOME": self.tmp.name, "INTENTSQL_NO_DOTENV": "1"})
        self.env.start()
        from intentsql import config, jev, web
        config.settings.cache_clear()
        jev.reset_shared_client()
        self.web = web
        self.client = TestClient(web.app)
        self.seen = []
        self.endpoint(system_one)

    def tearDown(self):
        from intentsql import config, jev
        self.transport.stop()
        self.env.stop()
        config.settings.cache_clear()
        jev.reset_shared_client()
        self.tmp.cleanup()

    def endpoint(self, handler):
        """Route every request the decision-model client makes to ``handler``."""
        if getattr(self, "transport", None):
            self.transport.stop()
        from intentsql import jev

        def recording(request):
            self.seen.append(request)
            return handler(request)
        self.transport = mock.patch.object(jev.httpx, "Client",
                                           lambda **_: REAL_CLIENT(transport=httpx.MockTransport(recording)))
        self.transport.start()
        jev.reset_shared_client()

    def add(self, **fields):
        body = {"label": "Lab model", "url": "https://lab.example/v1/systemone", "model": "lab-1",
                "api_key": KEY, "select": True, **fields}
        return self.client.post("/api/custom-models", json=body)

    def card(self, data, name="custom:lab-model"):
        return next(p for p in data["providers"] if p["name"] == name)

    def test_add_test_edit_and_remove(self):
        response = self.add()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertNotIn(KEY, response.text)                        # the key never goes back
        self.assertEqual((data["saved"], data["provider"]), ("custom:lab-model", "custom:lab-model"))
        card = self.card(data)
        self.assertEqual((card["custom"], card["has_key"], card["state"], card["url"], card["kinds"]),
                         (True, True, "untested", "https://lab.example/v1/systemone", ["choice", "noul"]))
        built_in = self.card(data, "typesafe")
        self.assertFalse(built_in["custom"])

        tested = self.client.post("/api/connection/test").json()
        self.assertTrue(tested["ok"], tested.get("error"))
        self.assertEqual(tested["tested"], ["choice", "noul"])
        self.assertEqual(self.card(tested)["state"], "connected")
        sent = json.loads(self.seen[-1].content)
        self.assertEqual(self.seen[-1].headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(sent["model"], "lab-1")
        self.assertEqual({q["type"] for q in sent["questions"].values()}, {"choice", "noul"})

        # Same host: the key stays. Another host: the key is dropped, and the state is re-tested.
        same = self.add(id="lab-model", api_key=None, model="lab-2", select=False).json()
        self.assertEqual((self.card(same)["has_key"], self.card(same)["model"], self.card(same)["state"]),
                         (True, "lab-2", "untested"))
        moved = self.add(id="lab-model", api_key=None, url="https://elsewhere.example/decide").json()
        self.assertFalse(self.card(moved)["has_key"])

        stored = json.loads((Path(self.tmp.name) / "connection.json").read_text())
        self.assertEqual((Path(self.tmp.name) / "connection.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("custom:lab-model", stored["keys"])

        removed = self.client.delete("/api/custom-models/lab-model").json()
        self.assertEqual((removed["provider"], removed["active"]), ("", None))     # never demo
        self.assertFalse(any(p["custom"] for p in removed["providers"]))
        self.assertEqual(self.client.delete("/api/custom-models/lab-model").status_code, 404)

    def test_incompatible_endpoint_is_explained(self):
        self.add()
        self.endpoint(chat_completion)
        tested = self.client.post("/api/connection/test").json()
        self.assertFalse(tested["ok"])
        self.assertIn("not in the System One decision format", tested["error"])
        self.assertIn("top-level fields: id, choices", tested["error"])
        self.assertEqual(self.card(tested)["state"], "incompatible")
        self.assertEqual(self.card(tested)["state_text"], "Incompatible")

    def test_http_failures_are_explained(self):
        self.add()
        self.endpoint(lambda r: httpx.Response(404, json={"error": "no such route"}))
        error = self.client.post("/api/connection/test").json()["error"]
        self.assertIn("Check the URL and the model ID", error)
        self.endpoint(lambda r: httpx.Response(200, text="<html>hello</html>",
                                               headers={"content-type": "text/html"}))
        error = self.client.post("/api/connection/test").json()["error"]
        self.assertIn("not JSON", error)
        self.assertIn("web page", error)
        self.endpoint(lambda r: httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}}))
        tested = self.client.post("/api/connection/test").json()
        self.assertEqual(self.card(tested)["state"], "auth_failed")
        self.assertNotIn(KEY, json.dumps(tested))                    # redacted even when echoed

    def test_validation(self):
        cases = {
            "http://lab.example/v1": "https://",                                 # unencrypted, not local
            "https://user:pw@lab.example/v1": "credentials",
            "https://lab.example/v1?key=abc": "query",
            "ftp://lab.example/v1": "full URL",
            "lab.example": "full URL",
        }
        for url, message in cases.items():
            response = self.add(url=url)
            self.assertEqual(response.status_code, 400, url)
            self.assertIn(message, response.json()["detail"])
        self.assertEqual(self.add(url="http://127.0.0.1:8080/decide").status_code, 200)
        self.assertEqual(self.add(model="bad model id").status_code, 400)
        self.assertEqual(self.add(label="").status_code, 400)
        self.assertEqual(self.add(kinds=[]).status_code, 400)
        self.assertEqual(self.add(kinds=["choice", "essay"]).status_code, 400)
        self.assertEqual(self.add(api_key="has space in it").status_code, 400)
        self.assertEqual(self.add(max_chars=10).status_code, 400)

    def test_a_model_without_required_types_is_saved_but_not_used(self):
        refused = self.add(kinds=["choice", "score"])
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Noul", refused.json()["detail"])
        saved = self.add(kinds=["choice", "score"], select=False).json()
        self.assertEqual(self.card(saved)["state"], "incompatible")
        self.assertNotEqual(saved["provider"], "custom:lab-model")
        selecting = self.client.post("/api/connection", json={"provider": "custom:lab-model"})
        self.assertEqual(selecting.status_code, 400)

    def test_no_key_means_no_authorization_header(self):
        self.add(api_key=None, url="http://localhost:9000/decide")
        tested = self.client.post("/api/connection/test").json()
        self.assertTrue(tested["ok"], tested.get("error"))
        self.assertNotIn("authorization", self.seen[-1].headers)

    def test_score_is_tested_when_declared(self):
        self.add(kinds=["choice", "noul", "score"])
        tested = self.client.post("/api/connection/test").json()
        self.assertEqual(tested["tested"], ["choice", "noul", "score"])
        sent = json.loads(self.seen[-1].content)
        self.assertIn("score", {q["type"] for q in sent["questions"].values()})

    def test_questions_are_answered_by_the_custom_model_never_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            shop = make_shop(tmp)
            name = self.client.post("/api/databases/upload?name=shop.sqlite", content=shop.read_bytes()).json()["name"]
        self.add()
        result = self.client.post("/api/ask", json={"database": name, "question": "How many orders are there?"}).json()
        self.assertEqual(result["provider"], "custom:lab-model")
        self.assertIn(result["status"], ("answered", "ambiguous"))
        self.assertIsNone(result["confidence"])                     # no calibration for custom models
        self.endpoint(chat_completion)
        failed = self.client.post("/api/ask", json={"database": name, "question": "How many orders are there?"})
        self.assertNotEqual(failed.json().get("provider"), "demo")

    def test_environment_can_select_a_custom_model(self):
        self.add(select=False)
        from intentsql import config
        with mock.patch.dict(os.environ, {"INTENTSQL_PROVIDER": "custom:lab-model"}):
            config.settings.cache_clear()
            cfg = config.settings()
            self.assertEqual((cfg.provider, cfg.jev_model, cfg.locked_by_environment),
                             ("custom:lab-model", "lab-1", True))
            self.assertEqual(self.client.delete("/api/custom-models/lab-model").status_code, 409)
        config.settings.cache_clear()
        with mock.patch.dict(os.environ, {"INTENTSQL_PROVIDER": "custom:gone"}):
            config.settings.cache_clear()
            self.assertIn("no longer exists", config.settings().notice)
        config.settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
