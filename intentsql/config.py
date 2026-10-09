"""Runtime configuration from environment variables and an optional ``.env`` file.

Recognized variables (all optional):

``SYSTEM_ONE_API_KEY`` (or ``TYPESAFE_API_KEY``)  Jev API key.
``SYSTEM_ONE_URL``                                Endpoint, default TypeSafe's System One API.
``SYSTEM_ONE_MODEL``                              Model alias, default ``jev-latest``.
``INTENTSQL_JEV_CACHE``                           Directory for a development response cache.
``INTENTSQL_WORKSPACE``                           Where imported databases and settings live.
``INTENTSQL_NO_DOTENV``                           Set to 1 to ignore ``.env`` files.

Precedence: process environment, then settings saved from the web app's
Connection dialog (``<workspace>/connection.json``), then a ``.env`` file in the
current directory or the project root.  The key is never logged.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip().removeprefix("export ").strip()] = value
    return values


def _default_workspace() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "IntentSQL"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "intentsql"


@dataclass(frozen=True)
class Settings:
    jev_api_key: str
    jev_url: str
    jev_model: str
    jev_cache_dir: str | None
    workspace: Path


@lru_cache(maxsize=1)
def settings() -> Settings:
    file_values: dict[str, str] = {}
    dotenv = () if os.environ.get("INTENTSQL_NO_DOTENV") == "1" else (Path.cwd() / ".env",
                                                                         PROJECT_ROOT / ".env")
    for candidate in dotenv:
        if candidate.is_file():
            file_values = {**_read_env_file(candidate), **file_values}

    def env(name: str) -> str:
        return os.environ.get(name) or ""

    def get(name: str, default: str = "") -> str:
        return env(name) or file_values.get(name) or default

    workspace = Path(get("INTENTSQL_WORKSPACE") or _default_workspace()).expanduser()
    try:
        saved = json.loads((workspace / "connection.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return Settings(
        jev_api_key=(env("SYSTEM_ONE_API_KEY") or env("TYPESAFE_API_KEY") or saved.get("api_key")
                     or file_values.get("SYSTEM_ONE_API_KEY") or file_values.get("TYPESAFE_API_KEY") or ""),
        jev_url=env("SYSTEM_ONE_URL") or saved.get("url") or file_values.get("SYSTEM_ONE_URL")
        or "https://api.typesafe.ai/v1/systemone",
        jev_model=env("SYSTEM_ONE_MODEL") or saved.get("model") or file_values.get("SYSTEM_ONE_MODEL")
        or "jev-latest",
        jev_cache_dir=get("INTENTSQL_JEV_CACHE") or None,
        workspace=workspace,
    )
