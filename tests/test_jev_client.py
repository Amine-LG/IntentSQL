import json
import unittest

import httpx

from intentsql.jev import MAX_QUESTIONS_PER_REQUEST, JevClient, JevError, Usage, _chunk, choice, noul


def client_with(handler, **kwargs):
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

    def test_large_fan_out_is_chunked_and_merged(self):
        questions = {f"q{i}": noul(f"question {i}?") for i in range(MAX_QUESTIONS_PER_REQUEST + 5)}
        self.assertEqual(len(_chunk(questions)), 2)
        usage = Usage()
        answers = client_with(ok_answers).ask({}, questions, usage=usage)
        self.assertEqual(len(answers), len(questions))
        self.assertEqual((usage.calls, usage.rounds), (2, 1))


if __name__ == "__main__":
    unittest.main()
