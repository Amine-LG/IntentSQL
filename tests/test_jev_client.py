import json
import unittest

import httpx

from intentsql.jev import (
    MAX_QUESTIONS_PER_REQUEST,
    BudgetExceeded,
    JevClient,
    JevError,
    Usage,
    _chunk,
    choice,
    noul,
    score,
)


def client_with(handler, **kwargs):
    kwargs.setdefault("provider", "typesafe")
    client = JevClient(url="https://jev.test/v1/systemone", model="jev-test", api_key="k-secret",
                       cache_dir="", **kwargs)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def ok_answers(request):
    body = json.loads(request.content)
    answers = {}
    for key, q in body["questions"].items():
        if q["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.8}
        else:
            first = next(iter(q["criteria"]))
            answers[key] = {"type": "choice", "choice": first, "confidence": 0.9,
                            "probabilities": {k: (1.0 if k == first else 0.0) for k in q["criteria"]}}
    return httpx.Response(200, json={"model": "jev-test-1", "answers": answers,
                                     "usage": {"input_tokens": 10, "output_tokens": 2}})


class JevClientTest(unittest.TestCase):
    def test_question_builders_validate(self):
        with self.assertRaises(ValueError):
            choice("pick", {"only": "one option"})
        self.assertEqual(noul("yes?", true="y").payload(),
                         {"type": "noul", "instructions": "yes?", "criteria": {"true": "y"}})

    def test_answers_usage_and_auth_header(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return ok_answers(request)

        usage, log = Usage(), []
        answers = client_with(handler).ask({"request": "x"}, {"a": choice("q", {"x": "1", "y": "2"}),
                                                              "b": noul("q?")}, usage=usage, log=log)
        self.assertEqual(answers["a"].choice, "x")
        self.assertAlmostEqual(answers["b"].p, 0.8)
        self.assertEqual((usage.calls, usage.input_tokens, usage.rounds), (1, 10, 1))
        self.assertEqual(seen["auth"], "Bearer k-secret")
        self.assertNotIn("k-secret", json.dumps([e.__dict__ for e in log], default=str))

    def test_out_of_set_choice_is_rejected(self):
        def handler(request):
            return httpx.Response(200, json={"answers": {"a": {"type": "choice", "choice": "zzz"}}})

        with self.assertRaises(JevError):
            client_with(handler).ask({}, {"a": choice("q", {"x": "1", "y": "2"})}, usage=Usage())

    def test_retries_transient_errors_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503, headers={"retry-after": "0"})
            return ok_answers(request)

        usage = Usage()
        client_with(handler, max_retries=3).ask({}, {"a": noul("q?")}, usage=usage)
        self.assertEqual(usage.retries, 2)

    def test_auth_errors_are_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(401)

        with self.assertRaises(JevError) as ctx:
            client_with(handler).ask({}, {"a": noul("q?")}, usage=Usage())
        self.assertEqual(calls["n"], 1)
        self.assertIn("API key", str(ctx.exception))

    def test_exhausted_credits_are_explained_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(402, json={"detail": {"error_type": "billing_error"}})

        with self.assertRaises(JevError) as ctx:
            client_with(handler).ask({}, {"a": noul("q?")}, usage=Usage())
        self.assertEqual(calls["n"], 1)
        self.assertIn("no credits left", str(ctx.exception))

    def test_large_fan_out_is_chunked_and_merged(self):
        questions = {f"q{i}": noul(f"question {i}?") for i in range(MAX_QUESTIONS_PER_REQUEST + 5)}
        self.assertEqual(len(_chunk(questions)), 2)
        usage = Usage()
        answers = client_with(ok_answers).ask({}, questions, usage=usage)
        self.assertEqual(len(answers), len(questions))
        self.assertEqual((usage.calls, usage.rounds), (2, 1))


if __name__ == "__main__":
    unittest.main()


class ProviderTest(unittest.TestCase):
    """Request format, accounting and provenance per provider (no network)."""

    def test_liquid_request_score_and_provenance(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            body = json.loads(request.content)
            seen["body"] = body
            return httpx.Response(200, json={
                "model": "d1", "id": "req-1",
                "answers": {"team": {"type": "choice", "choice": "billing", "confidence": 1,
                                     "probabilities": {"billing": 1, "technical": 0}},
                            "urgency": {"type": "score", "score": 2, "confidence": 1,
                                        "probabilities": {"0": 0, "1": 0, "2": 1}},
                            "refund": {"type": "noul", "noul": 0.82}},
                "usage": {"input_tokens": 447, "output_tokens": 69, "cost": 0.000018774}})

        client = JevClient(api_key="k-secret", provider="liquid", cache_dir="")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        usage, log = Usage(), []
        answers = client.ask({"ticket": "charged twice"}, {
            "team": choice("Which team?", {"billing": "money", "technical": "bugs"}),
            "urgency": score("How urgent?", ["routine", "soon", "now"]),
            "refund": noul("Refund requested?")}, usage=usage, log=log)
        self.assertEqual(seen["url"], "https://api.liquid.ai/decisions/v1/systemone")
        self.assertEqual(seen["auth"], "Bearer k-secret")
        self.assertEqual(seen["body"]["model"], "d1")
        self.assertEqual(seen["body"]["questions"]["urgency"]["criteria"], ["routine", "soon", "now"])
        self.assertEqual(answers["team"].choice, "billing")
        self.assertEqual(answers["urgency"].score, 2.0)
        self.assertAlmostEqual(answers["refund"].p, 0.82)
        self.assertAlmostEqual(usage.cost_usd, 0.000018774)
        self.assertEqual(usage.models, ["d1"])
        self.assertEqual((log[0].provider, log[0].request_id), ("liquid", "req-1"))
        self.assertNotIn("k-secret", json.dumps([e.__dict__ for e in log], default=str))
        self.assertNotIn("k-secret", json.dumps(client.describe()))

    def test_budget_stops_requests(self):
        def handler(request):
            return httpx.Response(200, json={
                "model": "m", "answers": {"a": {"type": "noul", "noul": 0.5}},
                "usage": {"input_tokens": 10, "output_tokens": 1, "cost": 0.01}})

        client = JevClient(api_key="k", provider="liquid", cache_dir="", budget_usd=0.015)
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client.ask({}, {"a": noul("q?")}, usage=Usage())
        client.ask({}, {"a": noul("q?")}, usage=Usage())          # 0.02 spent: limit now reached
        with self.assertRaises(BudgetExceeded):
            client.ask({}, {"a": noul("q?")}, usage=Usage())

    def test_errors_name_the_provider_and_never_fall_back(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(402)

        client = JevClient(api_key="k", provider="liquid", cache_dir="")
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(JevError) as ctx:
            client.ask({}, {"a": noul("q?")}, usage=Usage())
        self.assertIn("Liquid AI", str(ctx.exception))
        self.assertEqual(ctx.exception.kind, "credits")
        self.assertEqual(set(calls), {"https://api.liquid.ai/decisions/v1/systemone"})
        from intentsql.jev import connection_state
        self.assertEqual(connection_state("liquid", "d1").state, "no_credits")

    def test_provider_selection_from_configuration(self):
        import os
        import tempfile
        from unittest import mock

        from intentsql import config
        with tempfile.TemporaryDirectory() as tmp:
            base = {"INTENTSQL_WORKSPACE": tmp, "INTENTSQL_NO_DOTENV": "1", "XDG_CONFIG_HOME": tmp,
                    "SYSTEM_ONE_API_KEY": "", "TYPESAFE_API_KEY": "", "LIQUID_API_KEY": "",
                    "INTENTSQL_PROVIDER": "", "INTENTSQL_MODEL": ""}
            cases = [({"LIQUID_API_KEY": "l", "SYSTEM_ONE_API_KEY": "t"}, "typesafe"),
                     ({"LIQUID_API_KEY": "l"}, "liquid"),
                     ({}, ""),                                     # nothing selected: never demo
                     ({"SYSTEM_ONE_API_KEY": "t", "INTENTSQL_PROVIDER": "liquid"}, "liquid"),
                     ({"INTENTSQL_PROVIDER": "mock"}, "demo"),
                     ({"INTENTSQL_PROVIDER": "openrouter"}, "")]   # no longer supported
            for env, expected in cases:
                with self.subTest(env=env), mock.patch.dict(os.environ, {**base, **env}):
                    config.settings.cache_clear()
                    cfg = config.settings()
                    self.assertEqual(cfg.provider, expected)
            config.settings.cache_clear()
