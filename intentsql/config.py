"""Runtime configuration from environment variables and an optional ``.env`` file.

Recognized variables (all optional):

``SYSTEM_ONE_API_KEY`` (or ``TYPESAFE_API_KEY``)  Jev API key.
``SYSTEM_ONE_URL``                                Endpoint, default TypeSafe's System One API.
``SYSTEM_ONE_MODEL``                              Model alias, default ``jev-latest``.
``INTENTSQL_JEV_CACHE``                           Directory for a development response cache.
``INTENTSQL_WORKSPACE``                           Where imported databases and settings live.

The ``.env`` file is read from the current directory or the project root.
Values already present in the environment win.  The key is never logged.
"""

from __future__ import annotations

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
    for candidate in (Path.cwd() / ".env", PROJECT_ROOT / ".env"):
        if candidate.is_file():
            file_values = {**_read_env_file(candidate), **file_values}

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name) or file_values.get(name) or default

    return Settings(
        jev_api_key=get("SYSTEM_ONE_API_KEY") or get("TYPESAFE_API_KEY"),
        jev_url=get("SYSTEM_ONE_URL", "https://api.typesafe.ai/v1/systemone"),
        jev_model=get("SYSTEM_ONE_MODEL", "jev-latest"),
        jev_cache_dir=get("INTENTSQL_JEV_CACHE") or None,
        workspace=Path(get("INTENTSQL_WORKSPACE") or _default_workspace()),
    )
