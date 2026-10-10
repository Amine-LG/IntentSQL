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
from intentsql.config import PROJECT_ROOT, settings
from intentsql.examples import examples
from intentsql.jev import reset_shared_client, shared_client
from intentsql.pipeline import answer
from intentsql.providers import PROVIDERS
from intentsql.schema import load_schema, quote_identifier
from intentsql.workspace import (
    WorkspaceError,
    delete_database,
    import_bytes,
    import_path,
    list_databases,
    resolve_database,
    save_connection,
)

log = logging.getLogger("intentsql.web")
STATIC_DIR = PROJECT_ROOT / "static"
CONSOLE_MAX_ROWS = 500
CONSOLE_TIMEOUT_S = 5.0
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "testserver")

app = FastAPI(title="IntentSQL", version=__version__)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTS))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Reject cross-site POST/DELETE (the app serves only its own page)."""
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in ("localhost", "127.0.0.1", "::1"):
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
    return {"ok": True, "version": __version__, "provider": client.provider.name, "model": client.model}


# ---- connection ---------------------------------------------------------------------

class ConnectionInput(BaseModel):
    provider: str | None = Field(default=None, max_length=40)
    url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)


@app.get("/api/connection")
def get_connection() -> dict[str, Any]:
    """The active provider and which providers have a key -- never a key itself."""
    cfg = settings()
    client = shared_client()
    return {**client.describe(), "requested": cfg.provider_requested,
            "providers": [{"name": p.name, "label": p.label, "model": p.model,
                           "has_key": p.name in cfg.keys_present or p.offline}
                          for p in PROVIDERS.values()]}


@app.post("/api/connection")
def set_connection(body: ConnectionInput) -> dict[str, Any]:
    if body.url and not body.url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise HTTPException(400, "Use an https:// endpoint (http is allowed only for localhost).")
    try:
        save_connection(body.url, body.model, body.api_key, provider=body.provider)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
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
        raise HTTPException(400, f"No API key is configured for {client.provider.label}. "
                                 "Open Settings (the gear icon) to add one or choose another provider.")
    result = answer(entry.path, body.question)
    return _safe(result.to_dict(debug=True))


@app.post("/api/ask/stream")
def ask_stream(body: AskInput) -> StreamingResponse:
    """The same answer as /api/ask, preceded by progress events (server-sent events)."""
    entry = _entry(body.database)
    client = shared_client()
    if not client.configured:
        raise HTTPException(400, f"No API key is configured for {client.provider.label}. "
                                 "Open Settings (the gear icon) to add one or choose another provider.")
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
        raise HTTPException(400, f"SQLite: {exc}") from exc
    finally:
        timer.cancel()
        conn.close()


@app.post("/api/sql")
def run_sql(body: SQLInput) -> dict[str, Any]:
    return _run_console(_entry(body.database).path, body.sql, tuple(body.params))
