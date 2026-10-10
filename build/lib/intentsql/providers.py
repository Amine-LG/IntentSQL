"""Jev providers: where decisions are sent, and how each endpoint differs.

All real providers speak the same System One protocol: a request with
``state`` and typed ``questions`` (choice / score / noul), a response with
``answers`` keyed by question.  They differ in endpoint, model identifier,
key, accounting (OpenRouter reports a cost per request) and limits.

Selection is explicit and visible: ``INTENTSQL_PROVIDER`` (or the web app's
settings) names a provider, or ``auto`` picks the first provider in
``AUTO_ORDER`` that has a key.  Nothing falls back to another provider at
run time; a failing provider raises an error that names it.  Benchmarks record
the provider and the model snapshot that answered every request.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str                    # typesafe | openrouter | openjev | mock
    label: str                   # shown in the UI and in reports
    url: str
    model: str
    key_variables: tuple[str, ...]
    max_chars_per_request: int   # keeps every request well inside the context window
    reports_cost: bool = False
    billing_hint: str = ""
    docs: str = ""

    @property
    def offline(self) -> bool:
        return self.name == "mock"


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider(
        "openrouter", "TypeSafe Jev 1.13 via OpenRouter",
        "https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13",
        ("OPENROUTER_API_KEY",), 60_000, reports_cost=True,
        billing_hint="Add credits at openrouter.ai/settings/credits.",
        docs="https://openrouter.ai/typesafe/jev-1.13/api"),
    "typesafe": Provider(
        "typesafe", "TypeSafe Jev (direct)",
        "https://api.typesafe.ai/v1/systemone", "jev-latest",
        ("SYSTEM_ONE_API_KEY", "TYPESAFE_API_KEY"), 90_000,
        billing_hint="Add credits in the TypeSafe console (Settings > Billing).",
        docs="https://docs.typesafe.ai/concepts/system-one"),
    "openjev": Provider(
        "openjev", "OpenJEV",
        "https://api.openjev.sh/v1/systemone", "openjev",
        ("OPENJEV_API_KEY",), 60_000,
        billing_hint="Check the OpenJEV dashboard for your key's limits.",
        docs="https://openjev.sh/docs"),
    "mock": Provider(
        "mock", "Offline mock (no model; deterministic defaults)",
        "", "mock", (), 10**9),
}
AUTO_ORDER = ("openrouter", "typesafe", "openjev")


def provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"Unknown provider {name!r}; use one of {', '.join(PROVIDERS)}") from None
