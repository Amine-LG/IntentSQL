# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's "Report a vulnerability" (Security tab) rather than in a public issue.

## How IntentSQL handles keys and data

- **Keys stay on the server.** A key is posted once from the setup dialog, or entered without echo with `intent-sql keys`. It is stored in the workspace settings file with owner-only permissions (0600). On Windows, which has no such mode bits, the file is in the per-user `%LOCALAPPDATA%\IntentSQL` folder. Keys can also come from environment variables or `~/.config/intentsql/providers.env`. A key is:
  - sent only to its own provider, over HTTPS;
  - never returned by the API;
  - never written to `localStorage`, logs, exchanges or benchmark reports.

  Provider error messages are shown with the key redacted. Tests check that a saved key never appears in API responses.
- **Local only by default.** `intent-sql serve` listens on 127.0.0.1. The server accepts only localhost `Host` headers, which defends against DNS rebinding. It rejects state-changing requests from other origins, so a web page cannot change settings or spend credits through it. When running the Docker image, publish the port on localhost only (`-p 127.0.0.1:7862:7862`).
- **Read-only data access.** Every query runs on a read-only SQLite connection (`mode=ro`, `query_only`), with row and time limits. Generated SQL binds every value as a parameter. Identifiers come only from the database's own schema and are quoted. Imported databases are copied into the workspace, and only the copy is read.
- **Custom endpoints.** The URL must use HTTPS, except on localhost, and may not contain credentials or a query string. The key is sent only as a bearer token to that endpoint, and redirects are not followed. Changing an endpoint's host drops its saved key, so a key never reaches a host it was not entered for. Replies are validated against the offered options before they are used.
- **No hidden fallback.** IntentSQL uses exactly the provider you chose. When it fails, the error names it. Demo mode never stands in for a model.
- **Spending guard.** `INTENTSQL_BUDGET_USD` stops requests once the reported (or, for Liquid, estimated) spend reaches the limit.

## Automated checks

CI (`.github/workflows/ci.yml`) has read-only permissions and uses no secrets. On every push it runs:
- **gitleaks** over the full Git history (pinned release, checksum-verified). `.gitleaks.toml` allowlists exactly one fake test key, by value, never by path.
- **pip-audit** on the pinned runtime and development dependencies.

`.pre-commit-config.yaml` offers the same gitleaks check as a local pre-commit hook.

## Pre-release audit (2026-10-10, 1.0.0-beta.1)

Scope:
- the whole repository: all 51 commits on all local branches and tags, and all Git objects, including unreachable ones;
- the working tree;
- the built wheel and sdist;
- the Docker build context.

Findings are reported without secret values.

| Check | Tool | Result |
|---|---|---|
| Secrets in Git history | gitleaks 8.21.2 (`git` mode, default rules) | No leaks. One finding was a fake test key (`tests/test_web.py`), now allowlisted by value |
| Real key values in any Git object | a script comparing all 585 blobs, commits and tags with the key values configured on the development machine and with provider key patterns (TypeSafe, Liquid, OpenRouter, generic bearer tokens) | 0 matches |
| Secrets in the working tree | detect-secrets 1.5.0, gitleaks `dir` | Only fake test keys, and the developer's own `.env`, which is git-ignored, untracked, and excluded from the Docker context and packages |
| Packages | inspection of the wheel and sdist contents | No `.env`, settings or credential files |
| Dependencies | pip-audit 2.7.3 | No known vulnerabilities in the pinned requirements (two development pins were upgraded during the audit) |
| Code | bandit 1.8.6 | No high-severity findings. Eight medium findings (SQL built from strings) were reviewed: each quotes identifiers from the database's own schema and runs read-only. One former case was changed to use `quote_identifier` |
| History hygiene | git | No stashes and no unreachable objects. A single author identity, whose public address already appears on the published `main`. The only path to outside material is the relative `../IntentSQL-artifacts` |
| Personal data | review | The example databases contain public data (CS50). The test and benchmark fixtures are synthetic. Spider data is not in the repository |
| Licenses | review | Code: MIT. Example databases: CC BY-NC-SA 4.0 (CS50), with a notice in `intentsql/data/NOTICE.md` and the README. Runtime dependencies (FastAPI, Uvicorn, httpx) are permissively licensed |

**Credentials used during development.** OpenRouter and OpenJEV keys were provided for development and both were rejected by their providers (expired / invalid). They never appeared in any commit, object, package or report. Deactivating them, and any other credential that was ever used locally, remains the owner's decision. No credential was rotated or revoked as part of this audit, and no history was rewritten.
