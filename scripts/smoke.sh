#!/usr/bin/env bash
# Offline smoke test of an installed IntentSQL: CLI and HTTP API, in demo mode.
# No API key is used and no model provider is contacted.
#
#   scripts/smoke.sh                      # uses intent-sql from PATH
#   INTENT_SQL=.venv/bin/intent-sql scripts/smoke.sh
set -euo pipefail

bin="${INTENT_SQL:-intent-sql}"
port="${SMOKE_PORT:-7899}"
tmp="$(mktemp -d)"
server=""
stubs=()
here="$(cd "$(dirname "$0")" && pwd)"
cleanup() { [ -n "$server" ] && kill "$server" 2>/dev/null; for p in "${stubs[@]}"; do kill "$p" 2>/dev/null; done; rm -rf "$tmp"; }
trap cleanup EXIT

# An isolated, key-free environment: no .env files, no saved settings.
export INTENTSQL_NO_DOTENV=1 XDG_CONFIG_HOME="$tmp/config" INTENTSQL_WORKSPACE="$tmp/workspace"
unset SYSTEM_ONE_API_KEY TYPESAFE_API_KEY LIQUID_API_KEY INTENTSQL_PROVIDER || true

"$bin" --version

# Without a model, asking fails clearly instead of falling back to demo answers.
if "$bin" ask cyberchase.db "How many episodes are there?" > "$tmp/none.txt"; then
  echo "FAIL: answered without a model"; exit 1
fi
grep -q "No model is selected" "$tmp/none.txt"

"$bin" keys demo
"$bin" provider --test
"$bin" schema dese.db > /dev/null
"$bin" ask cyberchase.db "How many episodes are there?" | tee "$tmp/ask.txt"
grep -q "answered · demo" "$tmp/ask.txt"
grep -q "^140" "$tmp/ask.txt"

"$bin" serve --port "$port" > "$tmp/server.log" 2>&1 &
server=$!
for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:$port/api/health" > "$tmp/health.json" 2>/dev/null && break
  sleep 0.2
done
cat "$tmp/health.json"; echo
grep -q '"provider":"demo"' "$tmp/health.json"
curl -fsS "http://127.0.0.1:$port/" > "$tmp/index.html"     # not piped into grep -q: SIGPIPE under pipefail
grep -q "<title>IntentSQL" "$tmp/index.html"
curl -fsS "http://127.0.0.1:$port/static/app.js" > /dev/null
curl -fsS -X POST "http://127.0.0.1:$port/api/ask" -H "Content-Type: application/json" \
  -d '{"database": "cyberchase.db", "question": "How many episodes are there?"}' > "$tmp/answer.json"
grep -q '"status":"answered"' "$tmp/answer.json"
grep -q '"provider":"demo"' "$tmp/answer.json"
# Cross-site writes are refused.
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Origin: https://example.com' \
  -H 'Content-Type: application/json' -d '{"provider":"demo"}' "http://127.0.0.1:$port/api/connection")"
[ "$code" = 403 ] || { echo "FAIL: cross-origin write returned $code"; exit 1; }

# Custom decision models, over real HTTP, against a local protocol stub (not a model).
stub_port=$((port + 1)); chat_port=$((port + 2))
python3 "$here/../tests/system_one_stub.py" "$stub_port" & stubs+=($!)
python3 "$here/../tests/system_one_stub.py" "$chat_port" --chat & stubs+=($!)
sleep 1
if "$bin" custom add "Plain HTTP" --url "http://example.com/decide" --model m > "$tmp/bad.txt"; then
  echo "FAIL: accepted plain http to a remote host"; exit 1
fi
"$bin" custom add "Stub" --url "http://127.0.0.1:$stub_port/decide" --model stub-1 --use
"$bin" provider --test | tee "$tmp/custom-test.txt"
grep -q "test passed" "$tmp/custom-test.txt"
"$bin" ask cyberchase.db "How many episodes are there?" | tee "$tmp/custom-ask.txt"
grep -q "custom:stub call" "$tmp/custom-ask.txt"
"$bin" custom add "Chat API" --url "http://127.0.0.1:$chat_port/v1/chat/completions" --model chat --use
if "$bin" provider --test > "$tmp/chat-test.txt"; then echo "FAIL: chat API passed the test"; exit 1; fi
grep -q "not in the System One decision format" "$tmp/chat-test.txt"
"$bin" custom remove custom:chat-api
"$bin" custom remove custom:stub
"$bin" provider > "$tmp/after.txt"
grep -q "No model selected" "$tmp/after.txt"          # removing the selected model never falls back to demo

echo "smoke: OK"
