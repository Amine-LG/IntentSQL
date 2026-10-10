"""A System One protocol stub for tests (standard library only). It is not a model.

It answers every question with a fixed choice (the first of "none", "no", "unordered",
"all", "other", "read", "A" that is offered, else the first option), a fixed yes/no
probability and level 0. It exists to test custom-model support end to end.

    python tests/system_one_stub.py 7881            # compatible endpoint at /decide
    python tests/system_one_stub.py 7882 --chat     # an incompatible, chat-style reply
    STUB_KEY=... python tests/system_one_stub.py    # require "Authorization: Bearer ..."
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFERRED = ("none", "no", "unordered", "all", "other", "read", "A")


def answer(question: dict) -> dict:
    if question["type"] == "noul":
        return {"noul": 0.7}
    if question["type"] == "score":
        return {"score": 0, "probabilities": {"0": 1.0}}
    keys = list(question["criteria"])
    pick = next((k for k in PREFERRED if k in keys), keys[0])
    rest = 0.25 / max(1, len(keys) - 1)
    return {"choice": pick, "probabilities": {k: (0.75 if k == pick else rest) for k in keys}}


class Handler(BaseHTTPRequestHandler):
    chat = False

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.chat:
            return self.reply(200, {"id": "chat-1", "choices": [{"message": {"content": "billing"}}]})
        if self.path != "/decide":
            return self.reply(404, {"error": {"message": "not found"}})
        key = os.environ.get("STUB_KEY")
        if key and self.headers.get("Authorization") != f"Bearer {key}":
            return self.reply(401, {"error": {"message": "invalid key"}})
        self.reply(200, {"model": f"{body.get('model')}-stub", "usage": {"input_tokens": 1},
                         "answers": {k: answer(q) for k, q in body.get("questions", {}).items()}})

    def reply(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    port = int(next((a for a in sys.argv[1:] if a.isdigit()), "7881"))
    Handler.chat = "--chat" in sys.argv
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
