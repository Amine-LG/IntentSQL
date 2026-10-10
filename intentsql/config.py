"""Runtime configuration from environment variables and local configuration files.

Recognized variables (all optional):

``INTENTSQL_PROVIDER``                            ``auto`` (default), ``openrouter``, ``typesafe``,
                                                  ``openjev`` or ``mock`` (see providers.py).
``OPENROUTER_API_KEY``                            Key for TypeSafe Jev via OpenRouter.
``SYSTEM_ONE_API_KEY`` (or ``TYPESAFE_API_KEY``)  Key for TypeSafe's own API.
``OPENJEV_API_KEY``                               Key for OpenJEV.
``SYSTEM_ONE_URL`` / ``SYSTEM_ONE_MODEL``         Override the endpoint / model of the selected provider.
``INTENTSQL_BUDGET_USD``                          Stop sending requests once reported cost reaches this.
``INTENTSQL_JEV_CACHE``                           Directory for a development response cache.
``INTENTSQL_WORKSPACE``                           Where imported databases and settings live.
``INTENTSQL_NO_DOTENV``                           Set to 1 to ignore ``.env``-style files.

Precedence: process environment, then settings saved from the web app
(``<workspace>/connection.json``), then ``~/.config/intentsql/providers.env``,
then a ``.env`` file in the current directory or the project root.  Keys are
never logged, returned by the API or written anywhere but the 0600 settings file.
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
    provider: str                       # resolved provider name
    provider_requested: str             # what was configured ("auto" or a name)
    jev_api_key: str                    # key of the resolved provider (never exposed)
    jev_url: str
    jev_model: str
    jev_cache_dir: str | None
    workspace: Path
    budget_usd: float | None = None
    keys_present: tuple[str, ...] = ()  # providers that have a key (names only)

    def key_for(self, name: str) -> str:
        return _keys(self.workspace).get(name, "")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "intentsql"


def _file_values() -> dict[str, str]:
    if os.environ.get("INTENTSQL_NO_DOTENV") == "1":
        return {}
    values: dict[str, str] = {}
    for candidate in (PROJECT_ROOT / ".env", Path.cwd() / ".env", config_dir() / "providers.env"):
        if candidate.is_file():
            values.update(_read_env_file(candidate))      # later files win
    return values


def _saved(workspace: Path) -> dict:
    try:
        data = json.loads((workspace / "connection.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _keys(workspace: Path) -> dict[str, str]:
    """Each provider's key: environment, then saved settings, then config files."""
    from intentsql.providers import PROVIDERS
    files = _file_values()
    saved = _saved(workspace)
    raw = saved.get("keys")
    saved_keys: dict[str, str] = raw if isinstance(raw, dict) else {}
    if saved.get("api_key") and "typesafe" not in saved_keys:
        saved_keys = {**saved_keys, "typesafe": saved["api_key"]}     # settings from v1.0 alphas
    keys = {}
    for name, info in PROVIDERS.items():
        value = next((os.environ[v] for v in info.key_variables if os.environ.get(v)), "")
        value = value or saved_keys.get(name, "")
        value = value or next((files[v] for v in info.key_variables if files.get(v)), "")
        if value:
            keys[name] = value
    return keys


@lru_cache(maxsize=1)
def settings() -> Settings:
    from intentsql.providers import AUTO_ORDER, PROVIDERS
    files = _file_values()

    def get(name: str) -> str:
        return os.environ.get(name) or files.get(name) or ""

    workspace = Path(get("INTENTSQL_WORKSPACE") or _default_workspace()).expanduser()
    saved = _saved(workspace)
    keys = _keys(workspace)
    requested = (os.environ.get("INTENTSQL_PROVIDER") or saved.get("provider")
                 or files.get("INTENTSQL_PROVIDER") or "auto").strip().lower()
    if requested == "auto":
        name = next((n for n in AUTO_ORDER if keys.get(n)), AUTO_ORDER[0])
    else:
        name = requested if requested in PROVIDERS else AUTO_ORDER[0]
    info = PROVIDERS[name]
    overrides = name == "typesafe"      # legacy endpoint/model overrides apply to TypeSafe direct
    url = (get("SYSTEM_ONE_URL") or saved.get("url") or "") if overrides else ""
    model = (get("SYSTEM_ONE_MODEL") or saved.get("model") or "") if overrides else ""
    budget = get("INTENTSQL_BUDGET_USD")
    return Settings(
        provider=name, provider_requested=requested,
        jev_api_key=keys.get(name, ""), jev_url=url or info.url, jev_model=model or info.model,
        jev_cache_dir=get("INTENTSQL_JEV_CACHE") or None, workspace=workspace,
        budget_usd=float(budget) if budget else None,
        keys_present=tuple(n for n in PROVIDERS if keys.get(n)),
    )
