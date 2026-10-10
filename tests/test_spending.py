"""The spending guard: per-attempt reservations, persistence across runs, concurrency."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from intentsql.jev import BudgetExceeded, JevClient, JevError, Usage, noul
from intentsql.spending import Budget, LimitReached, reservation


def answer(request, tokens=1000):
    return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"a": {"noul": 0.5}},
                                     "usage": {"input_tokens": tokens, "output_tokens": 0}})


class SpendingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.json"

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, handler, limit=1.0, retries=2):
        client = JevClient(url="https://jev.test/v1/systemone", model="jev-latest", api_key="k-secret",
                           provider="typesafe", cache_dir="", max_retries=retries)
        client.budget = Budget(limit, self.ledger)
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        return client

    def test_ledger_persists_and_its_limit_cannot_be_raised(self):
        first = Budget(1.0, self.ledger)
        first.settle(first.reserve(0.4), 0.4, 1000)
        second = Budget(5.0, self.ledger)                 # a new run asking for more
        self.assertEqual(second.limit, 1.0)
        self.assertAlmostEqual(second.spent, 0.4)
        with self.assertRaises(LimitReached):
            second.reserve(0.7)
        lower = Budget(0.5, self.ledger)
        self.assertEqual(Budget(None, self.ledger).limit, 0.5)
        self.assertEqual(json.loads(self.ledger.read_text(encoding="utf-8"))["requests"], 1)
        self.assertIsNotNone(lower)

    def test_concurrent_reservations_never_pass_the_limit(self):
        budget = Budget(1.0, self.ledger)
        granted = []

        def take():
            try:
                granted.append(budget.reserve(0.1))
            except LimitReached:
                pass
        threads = [threading.Thread(target=take) for _ in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(granted), 10)

    def test_an_answer_is_counted_from_its_reported_tokens(self):
        client = self.client(answer)
        client.ask({}, {"a": noul("q?")}, usage=Usage())
        snap = client.budget.snapshot()
        self.assertAlmostEqual(snap["spent_usd"], 1000 * 0.042 / 1e6 * 1.10)
        self.assertEqual((snap["reserved_usd"], snap["requests"], snap["input_tokens"]), (0.0, 1, 1000))

    def test_retried_server_errors_and_timeouts_are_counted_rejections_are_not(self):
        calls = []

        def flaky(request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadTimeout("slow", request=request)
            if len(calls) == 2:
                return httpx.Response(503)
            return answer(request)
        client = self.client(flaky)
        client.ask({}, {"a": noul("q?")}, usage=Usage())
        body = len(json.dumps({"model": "jev-latest", "state": {},
                               "questions": {"a": noul("q?").payload()}}, ensure_ascii=False))
        upper = reservation(body, 0.042)
        snap = client.budget.snapshot()
        self.assertEqual(snap["requests"], 3)
        self.assertAlmostEqual(snap["spent_usd"], 2 * upper + 1000 * 0.042 / 1e6 * 1.10)

        rejected = self.client(lambda r: httpx.Response(402))
        before = rejected.budget.snapshot()["spent_usd"]
        with self.assertRaises(JevError):
            rejected.ask({}, {"a": noul("q?")}, usage=Usage())
        self.assertAlmostEqual(rejected.budget.snapshot()["spent_usd"], before)

    def test_answers_without_usage_keep_the_upper_bound(self):
        client = self.client(lambda r: httpx.Response(200, json={"answers": {"a": {"noul": 0.5}}}))
        client.ask({}, {"a": noul("q?")}, usage=Usage())
        self.assertGreater(client.budget.snapshot()["spent_usd"], 0)

    def test_requests_stop_before_the_limit(self):
        client = self.client(lambda r: answer(r, tokens=11_000_000), limit=0.5)
        client.ask({}, {"a": noul("q?")}, usage=Usage())              # 11M tokens: $0.51 counted
        with self.assertRaises(BudgetExceeded):
            client.ask({}, {"a": noul("q?")}, usage=Usage())


if __name__ == "__main__":
    unittest.main()
