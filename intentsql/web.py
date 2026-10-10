"""Local web app and JSON API.

Run with ``intent-sql serve`` (binds 127.0.0.1).  The app is read-only with
respect to databases: questions compile to SELECT statements executed on
read-only connections, and the SQL console rejects anything but reads.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from intentsql import __version__
from intentsql.config import PACKAGE_DIR, settings
from intentsql.examples import examples
from intentsql.jev import (
    TEST_STATE,
    JevClient,
    JevError,
    Usage,
    connection_state,
    connection_test_questions,
    reset_shared_client,
    shared_client,
)
from intentsql.pipeline import answer
from intentsql.providers import CUSTOM_PREFIX, missing_kinds
from intentsql.schema import load_schema, quote_identifier
from intentsql.workspace import (
    WorkspaceError,
    delete_database,
    import_bytes,
    import_path,
    list_databases,
    remove_custom_model,
    resolve_database,
    save_connection,
    save_custom_model,
)

log = logging.getLogger("intentsql.web")
STATIC_DIR = PACKAGE_DIR / "static"
CONSOLE_MAX_ROWS = 500
CONSOLE_TIMEOUT_S = 5.0
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")

app = FastAPI(title="IntentSQL", version=__version__)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTS))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Reject cross-site POST/DELETE: the origin must be this very server (scheme aside, the
    same host and port), so another local web app on a different port cannot use it either."""
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
            return JSONResponse({"detail": "Cross-origin request rejected."}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


def _safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def _entry(name: str):
    try:
        return resolve_database(name)
    except WorkspaceError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---- pages ------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    client = shared_client()
    return {"ok": True, "version": __version__, "provider": client.provider.name or None,
            "model": client.model or None}


# ---- connection ---------------------------------------------------------------------

class ConnectionInput(BaseModel):
    provider: str | None = Field(default=None, max_length=60)
    model: str | None = Field(default=None, max_length=100)
    api_key: str | None = Field(default=None, max_length=500)
    clear_key: bool = False


STATE_TEXT = {
    "not_configured": "Not configured", "untested": "Ready to test", "connected": "Connected",
    "auth_failed": "Key rejected", "no_credits": "No credits", "unavailable": "Unavailable",
    "budget_reached": "Spending limit reached", "error": "Error", "demo": "Demo mode",
    "incompatible": "Incompatible",
}


def _state(info: Any, model: str, has_key: bool) -> dict[str, Any]:
    """A provider's real status: never 'connected' without a successful answer."""
    name = info.name
    if name == "demo":
        return {"state": "demo", "message": "Fixed default decisions: no model reads your question."}
    if info.problem:
        return {"state": "incompatible", "message": info.problem}
    missing = missing_kinds(info)
    if missing:
        return {"state": "incompatible",
                "message": f"IntentSQL asks {' and '.join(k.title() for k in missing)} questions, which "
                           "this model is set up without."}
    if not has_key and not info.custom:
        return {"state": "not_configured", "message": "Add an API key to use this model."}
    known = connection_state(name, model)
    if known is None:
        return {"state": "untested", "message": "Saved. Test the connection to check that it answers in "
                "the format IntentSQL needs." if info.custom else
                "A key is saved. Test the connection to check it."}
    return {"state": known.state, "message": known.message, "model_snapshot": known.model_snapshot,
            "checked_at": round(known.checked_at)}


@app.get("/api/connection")
def get_connection() -> dict[str, Any]:
    """Every supported provider and the active one, with real states -- never a key."""
    cfg = settings()
    providers = []
    for p in cfg.providers.values():
        has_key = p.name in cfg.keys_present
        model = cfg.jev_model if p.name == cfg.provider else _saved_model(p.name)
        state = _state(p, model, has_key)
        entry = {
            "name": p.name, "label": p.label, "model_label": p.model_label, "model": model,
            "models": list(p.models), "needs_key": not p.offline and not p.custom, "has_key": has_key,
            "key_help": p.key_help, "docs": p.docs, "custom": p.custom, "price_per_mtok": p.price_per_mtok, **state,
            "state_text": STATE_TEXT.get(state["state"], state["state"])}
        if p.custom:
            entry.update({"id": p.name.removeprefix(CUSTOM_PREFIX), "url": p.url, "kinds": list(p.kinds),
                          "max_chars": p.max_chars_per_request})
        providers.append(entry)
    active = next((p for p in providers if p["name"] == cfg.provider), None)
    return {"active": active, "provider": cfg.provider, "locked_by_environment": cfg.locked_by_environment,
            "notice": cfg.notice, "providers": providers}


def _saved_model(name: str) -> str:
    from intentsql.workspace import saved_connection
    info = settings().providers[name]
    models = saved_connection().get("models")
    model = models.get(name) if isinstance(models, dict) else None
    return str(model if model in info.models else info.model)


@app.post("/api/connection")
def set_connection(body: ConnectionInput) -> dict[str, Any]:
    cfg = settings()
    if cfg.locked_by_environment and body.provider and body.provider != cfg.provider:
        raise HTTPException(409, "The model is set by the INTENTSQL_PROVIDER environment variable "
                                 "and cannot be changed here.")
    try:
        save_connection(provider=body.provider, api_key=body.api_key, model=body.model,
                        clear_key=body.clear_key)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
    reset_shared_client()
    return get_connection()


class TestInput(BaseModel):
    provider: str | None = Field(default=None, max_length=60)


@app.post("/api/connection/test")
def test_connection(body: TestInput | None = None) -> dict[str, Any]:
    """One tiny question per decision type the provider supports (one request), with the
    active provider or the one named. A pass means the answers came back in the right format;
    it says nothing about how well the model decides."""
    cfg = settings()
    name = (body.provider if body and body.provider else None) or cfg.provider
    if name not in cfg.providers:
        raise HTTPException(404, "Unknown provider")
    if cfg.providers[name].offline:
        return {**get_connection(), "ok": True}
    # A fresh client without the development cache: a test must reach the endpoint.
    client = JevClient(provider=name, model=cfg.jev_model if name == cfg.provider else _saved_model(name),
                       cache_dir="")
    usage = Usage()
    try:
        client.ask(TEST_STATE, connection_test_questions(client.provider.kinds), usage=usage)
    except JevError as exc:
        return {**get_connection(), "ok": False, "error": str(exc)}
    finally:
        client.close()
    return {**get_connection(), "ok": True, "tested": list(client.provider.kinds), "usage": usage.as_dict()}


class CustomModelInput(BaseModel):
    id: str | None = Field(default=None, max_length=40)       # None: add a new one
    label: str = Field(max_length=60)
    url: str = Field(max_length=400)
    model: str = Field(max_length=120)
    kinds: list[str] = Field(default_factory=lambda: ["choice", "noul"], max_length=3)
    max_chars: int | None = None
    api_key: str | None = Field(default=None, max_length=600)
    clear_key: bool = False
    select: bool = False


@app.post("/api/custom-models")
def save_custom(body: CustomModelInput) -> dict[str, Any]:
    """Add or edit a custom decision model. The key, if any, is stored server-side only."""
    cfg = settings()
    if body.select and cfg.locked_by_environment and (not body.id or CUSTOM_PREFIX + body.id != cfg.provider):
        raise HTTPException(409, "The model is set by the INTENTSQL_PROVIDER environment variable "
                                 "and cannot be changed here.")
    try:
        name = save_custom_model(slug=body.id, label=body.label, url=body.url, model=body.model,
                                 kinds=body.kinds, max_chars=body.max_chars, api_key=body.api_key,
                                 clear_key=body.clear_key, select=body.select)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
    reset_shared_client()
    return {**get_connection(), "saved": name}


@app.delete("/api/custom-models/{model_id}")
def delete_custom(model_id: str) -> dict[str, Any]:
    cfg = settings()
    if cfg.locked_by_environment and cfg.provider == CUSTOM_PREFIX + model_id:
        raise HTTPException(409, "This model is selected by the INTENTSQL_PROVIDER environment variable.")
    try:
        remove_custom_model(model_id)
    except WorkspaceError as exc:
        raise HTTPException(404, str(exc)) from exc
    reset_shared_client()
    return get_connection()


# ---- databases ------------------------------------------------------------------------

@app.get("/api/databases")
def databases() -> list[dict[str, Any]]:
    return [entry.describe() for entry in list_databases()]


class ImportPath(BaseModel):
    path: str = Field(max_length=4096)


@app.post("/api/databases/import")
def import_from_path(body: ImportPath) -> dict[str, Any]:
    try:
        return import_path(body.path).describe()
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/databases/upload")
async def upload(request: Request, name: str) -> dict[str, Any]:
    data = await request.body()
    try:
        return import_bytes(name, data).describe()
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/databases/{name}")
def remove_database(name: str) -> dict[str, Any]:
    try:
        delete_database(name)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"deleted": name}


@app.get("/api/databases/{name}/schema")
def schema(name: str) -> dict[str, Any]:
    entry = _entry(name)
    return _safe(load_schema(entry.path).describe())


@app.get("/api/databases/{name}/tables/{table}")
def preview(name: str, table: str, limit: int = 50) -> dict[str, Any]:
    entry = _entry(name)
    try:
        real = load_schema(entry.path).table(table).name
    except KeyError as exc:
        raise HTTPException(404, "Unknown table") from exc
    limit = max(1, min(limit, CONSOLE_MAX_ROWS))
    return _run_console(entry.path, f"SELECT * FROM {quote_identifier(real)} LIMIT {limit}")


# ---- questions ------------------------------------------------------------------------

class AskInput(BaseModel):
    database: str = Field(max_length=200)
    question: str = Field(min_length=1, max_length=2000)


@app.post("/api/ask")
def ask(body: AskInput) -> dict[str, Any]:
    entry = _entry(body.database)
    client = shared_client()
    if not client.configured:
        raise HTTPException(400, "No model is selected." if not client.provider.name else
                            f"No API key is saved for {client.provider.label}. "
                            "Use the model button at the top right to add one, or choose demo mode.")
    result = answer(entry.path, body.question)
    return _safe(result.to_dict(debug=True))


@app.post("/api/ask/stream")
def ask_stream(body: AskInput) -> StreamingResponse:
    """The same answer as /api/ask, preceded by progress events (server-sent events)."""
    entry = _entry(body.database)
    client = shared_client()
    if not client.configured:
        raise HTTPException(400, "No model is selected." if not client.provider.name else
                            f"No API key is saved for {client.provider.label}. "
                            "Use the model button at the top right to add one, or choose demo mode.")
    events: queue.Queue[tuple[str, Any] | None] = queue.Queue()

    def run() -> None:
        try:
            result = answer(entry.path, body.question, on_event=lambda e: events.put(("phase", e)))
            events.put(("result", _safe(result.to_dict(debug=True))))
        except Exception as exc:  # reported to the page, not raised into the worker thread
            log.exception("ask failed")
            events.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            events.put(None)

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while (item := events.get()) is not None:
            kind, data = item
            yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/databases/{name}/examples")
def database_examples(name: str) -> list[str]:
    return examples(load_schema(_entry(name).path))


# ---- read-only SQL console ------------------------------------------------------------

class SQLInput(BaseModel):
    database: str = Field(max_length=200)
    sql: str = Field(min_length=1, max_length=20_000)
    params: list[str | int | float | None] = Field(default_factory=list, max_length=200)


_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", None)
if _RECURSIVE is not None:
    _READ_ACTIONS.add(_RECURSIVE)


def _authorizer(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


def _run_console(path: Path, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    timer = threading.Timer(CONSOLE_TIMEOUT_S, conn.interrupt)
    timer.start()
    try:
        conn.set_authorizer(_authorizer)
        cursor = conn.execute(sql, params)
        columns = [d[0] for d in cursor.description or ()]
        rows = cursor.fetchmany(CONSOLE_MAX_ROWS + 1)
        return _safe({"columns": columns, "rows": [list(r) for r in rows[:CONSOLE_MAX_ROWS]],
                      "truncated": len(rows) > CONSOLE_MAX_ROWS})
    except sqlite3.Error as exc:
        if "not authorized" in str(exc) or "readonly" in str(exc):
            raise HTTPException(400, "Read-only: IntentSQL never adds, changes or deletes data. "
                                     "Use SELECT, WITH or EXPLAIN.") from exc
        raise HTTPException(400, f"SQLite: {exc}") from exc
    finally:
        timer.cancel()
        conn.close()


@app.post("/api/sql")
def run_sql(body: SQLInput) -> dict[str, Any]:
    return _run_console(_entry(body.database).path, body.sql, tuple(body.params))
