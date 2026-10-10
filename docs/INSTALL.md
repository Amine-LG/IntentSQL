# Installing and configuring IntentSQL

## Requirements

- Python 3.10 or newer (verified locally on 3.12; CI covers 3.10, 3.12 and 3.13), or Docker.
- A key for a decision model. TypeSafe Jev is recommended, at [console.typesafe.ai](https://console.typesafe.ai). Without a key, demo mode shows how the app works but does not understand questions.

## From a clone

```bash
git clone https://github.com/Amine-LG/IntentSQL.git && cd IntentSQL
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/intent-sql serve                    # http://127.0.0.1:7862
.venv/bin/intent-sql serve --port 8000        # another port
```

The server listens on 127.0.0.1 only. It accepts requests addressed to `localhost`, `127.0.0.1` or `[::1]`, and state-changing requests only from its own page.

## Docker

```bash
docker build -t intentsql .
docker run --rm -p 127.0.0.1:7862:7862 -v intentsql-data:/data intentsql
```

- The image runs as an unprivileged user and keeps its workspace (imported databases, settings, keys) in the `/data` volume.
- Publish the port on `127.0.0.1` only.
- To configure the model from the environment instead of the app: `-e INTENTSQL_PROVIDER=typesafe -e SYSTEM_ONE_API_KEY`.

## Connecting a model

In the app:
1. Click the model button at the top right.
2. Choose a model, paste its key and click **Save and use**.
3. Click **Test connection**.

On the command line:

```bash
intent-sql keys typesafe            # or: liquid, demo, custom:<id>; the key is typed without echo
intent-sql provider --test          # one tiny question of each decision type
intent-sql custom add "Name" --url https://… --model ID [--key] [--use]    # your own endpoint (docs/CUSTOM_MODELS.md)
```

## Configuration

All variables are optional.

| Variable | Meaning |
|---|---|
| `INTENTSQL_PROVIDER` | `typesafe`, `liquid`, `demo` or `custom:<id>`. Locks the choice; the app then says so |
| `SYSTEM_ONE_API_KEY` (or `TYPESAFE_API_KEY`), `LIQUID_API_KEY` | Keys for the built-in providers |
| `INTENTSQL_BUDGET_USD` | Spending limit: a request is not sent if it could pass the limit |
| `INTENTSQL_SPEND_LEDGER` | A file that keeps spending totals, so the limit holds across runs |
| `INTENTSQL_WORKSPACE` | Where imported databases and settings live. Default: `~/.local/share/intentsql`, or `%LOCALAPPDATA%\IntentSQL` on Windows |
| `INTENTSQL_URL`, `INTENTSQL_MODEL` | Advanced: override a built-in provider's endpoint or model (https, or http on localhost) |
| `INTENTSQL_NO_DOTENV=1` | Ignore `.env` files |

Settings saved in the app go to `<workspace>/connection.json`, which only you can read (mode 0600).

## Troubleshooting

| You see | Do this |
|---|---|
| *Not configured* | Add a key in the model dialog, or run `intent-sql keys <provider>` |
| *Key rejected* | Create a new key in the provider's console and save it |
| *No credits* | Add credits in the provider's console. IntentSQL never buys credits |
| *Incompatible* (custom model) | The endpoint does not answer in the System One format; see docs/CUSTOM_MODELS.md |
| *Spending limit reached* | Raise `INTENTSQL_BUDGET_USD`. With a ledger, its stored limit can only be lowered, so edit or replace the ledger file |
| "Locked by INTENTSQL_PROVIDER" | Unset the variable to choose the model in the app |
