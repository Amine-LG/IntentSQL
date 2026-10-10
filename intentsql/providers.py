"""Decision-model providers: where IntentSQL sends its bounded questions.

Both supported providers speak the same System One protocol: a request with
``state`` and typed ``questions`` (choice / score / noul), a response with
``answers`` keyed by question.  They differ in endpoint, model identifiers and key.

Selection is explicit and visible.  A provider is chosen in the app's settings,
by ``INTENTSQL_PROVIDER`` (which then locks the choice), or, when nothing is chosen,
the first provider that has a key.  Nothing ever switches provider at run time,
and the demo provider is used only when it is chosen: it is never a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass


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
        max_chars_per_request=90_000, price_per_mtok=0.042,
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


def provider(name: str) -> Provider:
    try:
        return PROVIDERS[ALIASES.get(name, name)]
    except KeyError:
        raise ValueError(f"Unknown provider {name!r}; use one of {', '.join(PROVIDERS)}") from None
