"""Runtime configuration from environment variables and local configuration files.

Recognized variables (all optional):

``INTENTSQL_PROVIDER``                            ``typesafe``, ``liquid``, ``demo`` or ``custom:<id>``
                                                  (a custom model saved in the app or with
                                                  ``intent-sql custom add``); locks the choice
                                                  (otherwise it is made in the app).
``SYSTEM_ONE_API_KEY`` (or ``TYPESAFE_API_KEY``)  Key for TypeSafe Jev.
``LIQUID_API_KEY``                                Key for Liquid AI d1.
``INTENTSQL_MODEL`` / ``INTENTSQL_URL``           Advanced: override the model / endpoint of the
                                                  selected built-in provider (never a custom model;
                                                  https, or http on localhost).
``INTENTSQL_BUDGET_USD``                          Spending limit: a request is not sent if it could pass
                                                  it (see intentsql/spending.py).
``INTENTSQL_SPEND_LEDGER``                        A file that keeps the spending totals across runs and
                                                  processes, so the limit is cumulative.
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
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent          # the source checkout (benchmarks, .env); absent when installed


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
    provider: str                       # resolved provider name; "" when no model is selected
    provider_requested: str             # what was configured ("auto" or a name)
    jev_api_key: str = field(repr=False)   # key of the resolved provider (never exposed)
    jev_url: str
    jev_model: str
    jev_cache_dir: str | None
    workspace: Path
    budget_usd: float | None = None
    spend_ledger: str | None = None      # file with cumulative spending totals (optional)
    keys_present: tuple[str, ...] = ()  # providers that have a key (names only)
    locked_by_environment: bool = False # INTENTSQL_PROVIDER is set: the app cannot change it
    notice: str = ""                    # e.g. a saved provider that is no longer supported
    providers: Any = None               # name -> Provider: built-in, then the user's custom models

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


def _custom(saved: dict) -> dict[str, Any]:
    """The user's custom models (name -> Provider), from the saved settings."""
    from intentsql.providers import CUSTOM_PREFIX, custom_provider
    raw = saved.get("custom")
    entries = raw if isinstance(raw, dict) else {}
    return {CUSTOM_PREFIX + str(slug): custom_provider(str(slug), entry)
            for slug, entry in entries.items() if isinstance(entry, dict)}


def all_providers() -> dict[str, Any]:
    """Built-in providers and the user's custom models."""
    return settings().providers


def _keys(workspace: Path) -> dict[str, str]:
    """Each provider's key: environment, then saved settings, then config files.
    Custom models' keys come from the saved settings only."""
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
    for name in _custom(saved):
        if isinstance(saved_keys.get(name), str) and saved_keys[name]:
            keys[name] = saved_keys[name]
    return {name: value for name, value in keys.items() if isinstance(value, str)}


@lru_cache(maxsize=1)
def settings() -> Settings:
    from intentsql.providers import ALIASES, AUTO_ORDER, PROVIDERS, url_problem
    files = _file_values()

    def get(name: str) -> str:
        return os.environ.get(name) or files.get(name) or ""

    workspace = Path(get("INTENTSQL_WORKSPACE") or _default_workspace()).expanduser()
    saved = _saved(workspace)
    keys = _keys(workspace)
    providers = {**PROVIDERS, **_custom(saved)}
    environment = (os.environ.get("INTENTSQL_PROVIDER") or "").strip().lower()
    requested = (environment or str(saved.get("provider") or "") or files.get("INTENTSQL_PROVIDER")
                 or "auto").strip().lower()
    requested = ALIASES.get(requested, requested)
    notice = ""
    # An explicit choice that does not exist selects no model: it never falls through to
    # whichever provider happens to have a key ("auto" is used only when asked for).
    if requested not in ("auto", "none") and requested not in providers:
        notice = (f"The custom model '{requested[7:]}' does not exist; no model is selected. Choose one."
                  if requested.startswith("custom:") else
                  f"The provider '{requested}' is not supported in this version; no model is selected. "
                  "Choose one.")
        requested = "none"
    if requested == "auto":
        name = next((n for n in AUTO_ORDER if keys.get(n)), "")
    elif requested == "none":
        name = ""
    else:
        name = requested
    info = providers.get(name)
    url = model = ""
    if info is not None and not info.custom:
        # Advanced overrides from the environment, for built-in providers only (a custom
        # model's key goes only to its own URL). An override must meet the same URL rules.
        url = get("INTENTSQL_URL") or (get("SYSTEM_ONE_URL") if name == "typesafe" else "")
        if url and url_problem(url):
            notice = f"Ignored the endpoint override in INTENTSQL_URL: {url_problem(url)}"
            url = ""
        raw_models = saved.get("models")
        chosen = raw_models.get(name, "") if isinstance(raw_models, dict) else ""
        model = (get("INTENTSQL_MODEL") or (get("SYSTEM_ONE_MODEL") if name == "typesafe" else "")
                 or (chosen if chosen in info.models else ""))
    budget = get("INTENTSQL_BUDGET_USD")
    return Settings(
        provider=name, provider_requested=requested,
        jev_api_key=keys.get(name, ""), jev_url=url or (info.url if info else ""),
        jev_model=model or (info.model if info else ""),
        jev_cache_dir=get("INTENTSQL_JEV_CACHE") or None, workspace=workspace,
        budget_usd=float(budget) if budget else None, spend_ledger=get("INTENTSQL_SPEND_LEDGER") or None,
        keys_present=tuple(n for n in providers if keys.get(n)),
        locked_by_environment=bool(environment), notice=notice, providers=providers,
    )
