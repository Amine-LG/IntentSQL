"""Decision-model providers: where IntentSQL sends its bounded questions.

The built-in providers speak the System One protocol: a request with ``state`` and
typed ``questions`` (choice / score / noul), a response with ``answers`` keyed by
question.  They differ in endpoint, model identifiers and key.  Users can add their
own endpoints for the same protocol ("custom models", stored in the workspace
settings); those are never assumed to work until a test answers correctly.

Selection is explicit and visible.  A provider is chosen in the app's settings,
by ``INTENTSQL_PROVIDER`` (which then locks the choice), or, when nothing is chosen,
the first provider that has a key.  Nothing ever switches provider at run time,
and the demo provider is used only when it is chosen: it is never a fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Provider:
    name: str                    # typesafe | liquid | demo
    label: str                   # shown in the UI and in reports
    model_label: str             # the model, as people know it
    url: str
    models: tuple[str, ...]      # supported model identifiers; the first is the default
    key_variables: tuple[str, ...]
    key_prefix: str = ""         # documented key prefix, used only to warn about typos
    key_help: str = ""           # where to get a key
    max_chars_per_request: int = 60_000
    reports_cost: bool = False
    # Listed input price in USD per million tokens, used only to *estimate* spend for the
    # budget guard when the provider does not report a cost (output tokens are free).
    price_per_mtok: float | None = None
    billing_hint: str = ""
    docs: str = ""
    custom: bool = False         # configured by the user, not verified by IntentSQL
    kinds: tuple[str, ...] = ("choice", "noul")   # decision types the endpoint supports
    problem: str = ""            # why a saved custom model cannot be used (e.g. an invalid URL)

    @property
    def offline(self) -> bool:
        return self.name == "demo"

    @property
    def model(self) -> str:
        return self.models[0]


PROVIDERS: dict[str, Provider] = {
    "typesafe": Provider(
        "typesafe", "TypeSafe", "Jev", "https://api.typesafe.ai/v1/systemone", ("jev-latest",),
        ("SYSTEM_ONE_API_KEY", "TYPESAFE_API_KEY"),
        key_help="Create a key in the TypeSafe console: https://console.typesafe.ai",
        max_chars_per_request=90_000, price_per_mtok=0.042, kinds=("choice", "noul", "score"),
        billing_hint="Add credits in the TypeSafe console (Settings > Billing).",
        docs="https://docs.typesafe.ai/concepts/system-one"),
    "liquid": Provider(
        "liquid", "Liquid AI", "d1", "https://api.liquid.ai/decisions/v1/systemone", ("d1", "d1:free"),
        ("LIQUID_API_KEY",), key_prefix="liquid_",
        key_help="Create a key in the Liquid console (Dashboard > API Keys): https://console.liquid.ai",
        price_per_mtok=0.04,
        billing_hint="Check your credits in the Liquid console.",
        docs="https://docs.liquid.ai/lfm/models/d1"),
    "demo": Provider(
        "demo", "Demo mode", "no model", "", ("demo",), (), max_chars_per_request=10**9),
}
AUTO_ORDER = ("typesafe", "liquid")
ALIASES = {"mock": "demo"}       # earlier name of the demo provider

KINDS = ("choice", "noul", "score")
REQUIRED_KINDS = ("choice", "noul")   # what IntentSQL asks; Score is not used yet
CUSTOM_PREFIX = "custom:"
CUSTOM_MAX_CHARS = 60_000


LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,100}$")


def url_problem(url: str) -> str:
    """Why a decision-endpoint URL is not acceptable ("" when it is): https, or http on
    loopback only; no credentials, query, fragment, whitespace or control characters."""
    if not isinstance(url, str) or not url or len(url) > 300 or any(c.isspace() or ord(c) < 32 for c in url):
        return "Enter the endpoint's full URL, for example https://example.com/v1/systemone."
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port                                  # noqa: B018 -- raises on an invalid port
    except ValueError:
        return "That is not a valid URL."
    if not host or parts.scheme not in ("https", "http"):
        return "Enter the endpoint's full URL, for example https://example.com/v1/systemone."
    if parts.username or parts.password:
        return "Do not put credentials in the URL; use the API key field."
    if parts.query or parts.fragment:
        return "The URL must not contain ?query or #fragment parts; put any key in the API key field."
    if parts.scheme == "http" and host not in LOOPBACK:
        return ("Use https:// (plain http:// is allowed only for localhost), so the key and "
                "your questions are not sent unencrypted.")
    return ""


def endpoint_identity(url: str) -> tuple[str, str, int | None]:
    """Scheme, host and port: a saved key belongs to exactly this."""
    try:
        parts = urlsplit(str(url))
        return parts.scheme.lower(), (parts.hostname or "").lower().rstrip("."), parts.port
    except ValueError:
        return "", "", None


def custom_provider(slug: str, entry: dict) -> Provider:
    """A user-configured endpoint, from its saved settings entry (re-validated on load:
    the settings file may have been edited by hand)."""
    model = str(entry.get("model") or "")
    url = str(entry.get("url") or "")
    raw_kinds = entry.get("kinds")
    kinds = tuple(k for k in KINDS if k in raw_kinds) if isinstance(raw_kinds, list) else REQUIRED_KINDS
    raw_size = entry.get("max_chars")
    max_chars = (raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool)
                 and 2_000 <= raw_size <= 1_000_000 else CUSTOM_MAX_CHARS)
    problem = url_problem(url)
    if not problem and not _MODEL_ID.match(model):
        problem = "The saved model ID is not valid."
    if problem:
        problem = f"Edit this model: {problem}"
    return Provider(
        CUSTOM_PREFIX + slug, str(entry.get("label") or slug)[:40], model, url,
        (model,), (), max_chars_per_request=max_chars,
        custom=True, kinds=kinds, problem=problem)


def missing_kinds(info: Provider) -> list[str]:
    """Decision types IntentSQL needs that this provider does not declare."""
    return [k for k in REQUIRED_KINDS if k not in info.kinds]


def provider(name: str) -> Provider:
    try:
        return PROVIDERS[ALIASES.get(name, name)]
    except KeyError:
        raise ValueError(f"Unknown provider {name!r}; use one of {', '.join(PROVIDERS)}") from None
