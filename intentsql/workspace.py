"""Local workspace: bundled and imported databases, saved connection settings.

Imported databases are copied into the workspace; originals are never opened
for writing.  Bundled databases are read from the repository's ``data``
directory.  The saved API key lives in ``connection.json`` (mode 0600) and is
never returned by the API.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intentsql.config import PROJECT_ROOT, settings

BUNDLED_DIR = PROJECT_ROOT / "data"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,120}$")
_LOCK = threading.Lock()
MAX_IMPORT_BYTES = 2 * 1024 ** 3


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class DatabaseEntry:
    name: str
    path: Path
    bundled: bool

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "bundled": self.bundled,
                "size_bytes": self.path.stat().st_size if self.path.exists() else None}


def workspace_dir() -> Path:
    path = settings().workspace
    (path / "databases").mkdir(parents=True, exist_ok=True)
    return path


def _imported_dir() -> Path:
    return workspace_dir() / "databases"


def list_databases() -> list[DatabaseEntry]:
    entries = []
    if BUNDLED_DIR.is_dir():
        for path in sorted(BUNDLED_DIR.glob("*.db")) + sorted(BUNDLED_DIR.glob("*.sqlite")):
            entries.append(DatabaseEntry(path.name, path, True))
    bundled = {e.name for e in entries}
    for path in sorted(_imported_dir().iterdir()):
        if path.is_file() and path.suffix in (".db", ".sqlite", ".sqlite3") and path.name not in bundled:
            entries.append(DatabaseEntry(path.name, path, False))
    return entries


def resolve_database(name: str) -> DatabaseEntry:
    if not _NAME.match(name or ""):
        raise WorkspaceError("Invalid database name.")
    for entry in list_databases():
        if entry.name == name:
            return entry
    raise WorkspaceError(f"Unknown database: {name}")


def _validate_sqlite(path: Path) -> None:
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise WorkspaceError("The file is not a SQLite database.")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        raise WorkspaceError(f"SQLite could not read the file: {exc}") from exc
    finally:
        conn.close()


def _unique_name(stem: str, suffix: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]", "_", stem).strip(" ._") or "database"
    existing = {e.name for e in list_databases()}
    name = f"{stem}{suffix}"
    counter = 2
    while name in existing:
        name = f"{stem}-{counter}{suffix}"
        counter += 1
    return name


def import_bytes(filename: str, data: bytes) -> DatabaseEntry:
    """Store an uploaded SQLite file as a new working copy."""
    if len(data) > MAX_IMPORT_BYTES:
        raise WorkspaceError("The file is too large to import.")
    suffix = Path(filename).suffix.lower()
    suffix = suffix if suffix in (".db", ".sqlite", ".sqlite3") else ".db"
    with _LOCK:
        target_dir = _imported_dir()
        fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".upload")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            _validate_sqlite(Path(tmp))
            name = _unique_name(Path(filename).stem, suffix)
            final = target_dir / name
            os.replace(tmp, final)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return DatabaseEntry(name, final, False)


def import_path(source: str) -> DatabaseEntry:
    """Copy a local SQLite file into the workspace (the original is untouched)."""
    path = Path(source).expanduser()
    if not path.is_file():
        raise WorkspaceError("No such file.")
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise WorkspaceError("The file is too large to import.")
    _validate_sqlite(path)
    with _LOCK:
        name = _unique_name(path.stem, path.suffix.lower() if path.suffix else ".db")
        final = _imported_dir() / name
        shutil.copy2(path, final)
    return DatabaseEntry(name, final, False)


def delete_database(name: str) -> None:
    entry = resolve_database(name)
    if entry.bundled:
        raise WorkspaceError("Bundled example databases cannot be deleted.")
    entry.path.unlink()


# ---- connection settings -------------------------------------------------------

def connection_file() -> Path:
    return workspace_dir() / "connection.json"


def saved_connection() -> dict[str, Any]:
    try:
        return json.loads(connection_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_connection(url: str | None, model: str | None, api_key: str | None) -> None:
    """Persist endpoint/model, and the key only when a new one is supplied."""
    current = saved_connection()
    if url is not None:
        current["url"] = url.strip()
    if model is not None:
        current["model"] = model.strip()
    if api_key:
        current["api_key"] = api_key.strip()
    path = connection_file()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(current, handle)
    os.replace(tmp, path)
    settings.cache_clear()
