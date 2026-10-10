# Custom decision models

IntentSQL is not tied to its built-in providers. You can connect any endpoint that implements the **System One decision protocol**, which is the protocol TypeSafe Jev and Liquid AI d1 use. Examples: your own deployment, a gateway, or a model you are developing.

Custom models are configured by you and **not verified by IntentSQL**. A successful *Test connection* means the endpoint answered in the right format. It says nothing about how well the model makes decisions; measure that with `scripts/release_eval.sh custom:<id>` before you rely on it. Confidence is not calibrated for custom models, so the app shows the raw verification score.

A general chat or completion API (for example an OpenAI-style `/chat/completions` URL) does **not** implement this protocol and will be reported as *Incompatible*.

## Adding one

**In the app:**
1. Open the model button at the top right.
2. Click **+ Custom model**.
3. Enter a name, the endpoint URL and the model ID, plus an API key if the endpoint needs one.
4. Click **Save and use**, or **Save only** to test it before switching.
5. Click **Test connection**.

The decision types and the request-size limit are under **Advanced**. You can edit or delete a custom model in the same place.

**On the command line:**

```bash
intent-sql custom add "My model" --url https://models.example.com/v1/systemone --model my-model-1 --key --use
intent-sql provider --test                   # one tiny question per decision type
intent-sql custom list
intent-sql custom add "My model" --edit custom:my-model --url https://new.example.com/v1/systemone --model my-model-2
intent-sql custom remove custom:my-model
```

**On a server:** `INTENTSQL_PROVIDER=custom:<id>` selects a saved custom model and locks the choice.

### Rules IntentSQL enforces

- **URLs.** Use `https://`. Plain `http://` is accepted only for `localhost`, `127.0.0.1` and `::1`. URLs may not contain credentials, a `?query` or a `#fragment`; put keys in the API key field.
- **Keys.** A key is optional. It is stored server-side in the workspace settings (owner-only file) and never returned to the browser. It is sent only as `Authorization: Bearer <key>` to that endpoint, and redirects are not followed. When you change an endpoint's scheme, host or port, its saved key is dropped, so a key is never sent to an address it was not entered for. The `INTENTSQL_URL` and `INTENTSQL_MODEL` overrides apply to built-in providers only.
- **Decision types.** IntentSQL asks **Choice** and **Noul** questions. A model saved without both can be kept, but not selected. **Score** is optional and not used by IntentSQL yet.
- **No fallback.** If a custom model fails, the error names it. IntentSQL never switches to another model or to demo mode. Deleting the selected custom model, or naming one that does not exist in `INTENTSQL_PROVIDER`, leaves no model selected.

## The protocol

IntentSQL sends `POST <url>` with `Content-Type: application/json`:

```json
{
  "model": "my-model-1",
  "state": {"request": "How many orders are paid?", "...": "any JSON: the context the questions refer to"},
  "questions": {
    "count": {
      "type": "choice",
      "instructions": "Does `request` ask to be told a count? If so, a count of what?",
      "criteria": {"t0": "how many order records there are", "none": "the request does not ask for a count"}
    },
    "verify_A": {
      "type": "noul",
      "instructions": {"question": "Does interpretation A describe exactly what `request` asks for?"}
    },
    "urgency": {
      "type": "score",
      "instructions": "How urgent is `ticket`?",
      "criteria": ["not urgent", "somewhat urgent", "very urgent"]
    }
  }
}
```

- `instructions` is a string or a JSON object. Names in backticks refer to fields of `state`.
- A `choice` question has 2 to 255 options in `criteria` (key → description).
- A `noul` question may carry `criteria` with `true` and/or `false` descriptions.
- A `score` question lists its levels in order, level 0 first.
- Many questions arrive in one request. Large sets are split into several requests, up to the model's request-size limit (60,000 characters by default).

The endpoint replies `200` with a JSON object containing one answer per question key:

```json
{
  "answers": {
    "count":    {"choice": "t0", "probabilities": {"t0": 0.92, "none": 0.08}, "confidence": 0.9},
    "verify_A": {"noul": 0.87},
    "urgency":  {"score": 1, "probabilities": {"0": 0.1, "1": 0.7, "2": 0.2}}
  },
  "model": "my-model-1-2026-10-01",
  "id": "req_123",
  "usage": {"input_tokens": 812, "output_tokens": 0, "cost": 0.00003}
}
```

| Field | Required | Meaning |
|---|---|---|
| `answers.<key>.choice` | yes, for Choice | one of the offered option keys |
| `answers.<key>.probabilities` | recommended, for Choice | a probability per option key. IntentSQL uses these to find close calls and build alternative readings; without them, the chosen option is taken as certain |
| `answers.<key>.noul` | yes, for Noul | probability of "yes", between 0 and 1 |
| `answers.<key>.score` | yes, for Score | the chosen level number |
| `model`, `id` | no | recorded as provenance in exchanges and benchmark reports |
| `usage.input_tokens`, `usage.output_tokens`, `usage.cost` | no | counted in usage. `cost` (USD) also counts toward `INTENTSQL_BUDGET_USD`; without it, IntentSQL cannot track a custom model's spending |

**Errors.** Reply with an HTTP status and, ideally, `{"error": {"message": "..."}}`. IntentSQL shows the message, with any key redacted.

| Status | What the app shows |
|---|---|
| 401, 403 | *Key rejected* |
| 402 | *No credits* |
| 404 | the URL or the model ID is wrong |
| 400, 415, 422 | the endpoint does not seem to implement the protocol |
| 408, 429, 500, 502–504 | retried, then *Unavailable* |

A `200` without a valid `answers` object, or with an answer outside the offered options, is reported as *Incompatible*, naming what was wrong.
