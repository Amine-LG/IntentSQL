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

from intentsql.config import PACKAGE_DIR, all_providers, settings

BUNDLED_DIR = PACKAGE_DIR / "data"
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
        data = json.loads(connection_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


_KEY_SHAPE = re.compile(r"^[A-Za-z0-9_\-.:]{16,300}$")
_CUSTOM_KEY_SHAPE = re.compile(r"^[\x21-\x7e]{8,500}$")       # printable ASCII, no spaces
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,100}$")


def _write_connection(current: dict[str, Any]) -> None:
    path = connection_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(current, handle)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    settings.cache_clear()


def save_connection(*, provider: str | None = None, api_key: str | None = None,
                    model: str | None = None, clear_key: bool = False) -> None:
    """Persist the chosen provider, its model and (when supplied) its key.

    Stored in <workspace>/connection.json with owner-only permissions.  Keys are kept per
    provider; obsolete global endpoint/model overrides from earlier versions are dropped
    (a stale ``model`` could otherwise follow the user to another provider)."""
    from intentsql.providers import ALIASES, missing_kinds
    providers = all_providers()
    current = saved_connection()
    for obsolete in ("url", "model", "api_key"):
        if obsolete == "api_key" and current.get("api_key"):
            current["keys"] = {"typesafe": current["api_key"], **_dict(current.get("keys"))}
        current.pop(obsolete, None)
    name = ALIASES.get(provider, provider) if provider else None
    if name is not None:
        if name not in providers:
            raise WorkspaceError(f"Unknown provider {provider}")
        if providers[name].problem:
            raise WorkspaceError(providers[name].problem)
        missing = missing_kinds(providers[name])
        if missing:
            raise WorkspaceError(f"{providers[name].label} cannot be used: IntentSQL asks "
                                 f"{' and '.join(k.title() for k in missing)} questions, which it "
                                 "does not support. Edit its decision types if it does.")
        current["provider"] = name
    target = name or settings().provider
    info = providers.get(target)
    if model:
        if info is None or model not in info.models:
            raise WorkspaceError(f"Unknown model {model} for {target or 'this provider'}")
        current["models"] = {**_dict(current.get("models")), target: model}
    keys = _dict(current.get("keys"))
    if clear_key and target:
        keys.pop(target, None)
    if api_key:
        key = api_key.strip()
        if info is None or info.offline:
            raise WorkspaceError("This provider does not use an API key.")
        _check_key(key, custom=info.custom)
        keys[target] = key
    current["keys"] = keys
    _write_connection(current)
    if target and (api_key or clear_key):
        from intentsql.jev import forget_state
        forget_state(target)          # a new or removed key has not been tested


def _check_key(key: str, *, custom: bool) -> None:
    if custom and not _CUSTOM_KEY_SHAPE.match(key):
        raise WorkspaceError("That does not look like an API key (8-500 visible characters, no spaces).")
    if not custom and not _KEY_SHAPE.match(key):
        raise WorkspaceError("That does not look like an API key (letters, digits and - _ . : only).")


def _check_url(url: str) -> str:
    from intentsql.providers import url_problem
    url = url.strip()
    problem = url_problem(url)
    if problem:
        raise WorkspaceError(problem)
    return url


def _slug(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:32] or "model"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def save_custom_model(*, slug: str | None, label: str, url: str, model: str,
                      kinds: list[str] | None = None, max_chars: int | None = None,
                      api_key: str | None = None, clear_key: bool = False, select: bool = False) -> str:
    """Add (slug None) or edit a custom decision model; returns its provider name.

    A saved key is dropped when the endpoint's scheme, host or port changes, so a key is
    never sent anywhere it was not entered for."""
    from intentsql.providers import CUSTOM_MAX_CHARS, CUSTOM_PREFIX, KINDS, endpoint_identity
    current = saved_connection()
    custom = _dict(current.get("custom"))
    label = " ".join(str(label).split())
    if not 1 <= len(label) <= 40:
        raise WorkspaceError("Give the model a name of 1-40 characters.")
    url = _check_url(url)
    model = model.strip()
    if not _MODEL_ID.match(model):
        raise WorkspaceError("Enter the model ID the endpoint expects (letters, digits and . _ : / @ + -).")
    if kinds is not None and not all(isinstance(k, str) for k in kinds):
        raise WorkspaceError(f"Decision types must be among {', '.join(KINDS)}.")
    chosen = [k for k in KINDS if k in (kinds if kinds is not None else ("choice", "noul"))]
    if not chosen or len(chosen) != len(set(kinds or chosen)):
        raise WorkspaceError(f"Decision types must be among {', '.join(KINDS)}.")
    if max_chars is not None and not 2_000 <= max_chars <= 1_000_000:
        raise WorkspaceError("The request size limit must be between 2,000 and 1,000,000 characters.")
    if slug is None:
        slug = _slug(label, set(custom))
    elif slug not in custom:
        raise WorkspaceError("Unknown custom model.")
    name = CUSTOM_PREFIX + slug
    keys = _dict(current.get("keys"))
    previous = _dict(custom.get(slug))
    if previous and endpoint_identity(str(previous.get("url", ""))) != endpoint_identity(url):
        keys.pop(name, None)
    if clear_key:
        keys.pop(name, None)
    if api_key:
        key = api_key.strip()
        _check_key(key, custom=True)
        keys[name] = key
    custom[slug] = {"label": label, "url": url, "model": model, "kinds": chosen,
                    "max_chars": max_chars or previous.get("max_chars") or CUSTOM_MAX_CHARS}
    current["custom"], current["keys"] = custom, keys
    if select:
        missing = [k for k in ("choice", "noul") if k not in chosen]
        if missing:
            raise WorkspaceError(f"IntentSQL asks {' and '.join(k.title() for k in missing)} questions; "
                                 "a model without them cannot be used.")
        current["provider"] = name
    _write_connection(current)
    from intentsql.jev import forget_state
    forget_state(name)
    return name


def remove_custom_model(slug: str) -> None:
    """Delete a custom model and its key. If it was selected, no model is selected afterwards:
    never demo, and never another provider the user did not choose."""
    from intentsql.providers import CUSTOM_PREFIX
    current = saved_connection()
    custom = _dict(current.get("custom"))
    if slug not in custom:
        raise WorkspaceError("Unknown custom model.")
    name = CUSTOM_PREFIX + slug
    del custom[slug]
    current["custom"] = custom
    _dict(current.get("keys")).pop(name, None)
    _dict(current.get("models")).pop(name, None)
    if current.get("provider") == name:
        current["provider"] = "none"
    _write_connection(current)
    from intentsql.jev import forget_state
    forget_state(name)
