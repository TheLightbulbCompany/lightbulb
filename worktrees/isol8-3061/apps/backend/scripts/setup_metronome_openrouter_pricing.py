"""Find-or-create the Metronome metric, product and rate that price managed OpenRouter usage.

The settlement Lambda ingests one ``openrouter_inference`` event per generation carrying the
raw provider cost as ``provider_cost_nanodollars`` (1 USD = 1e9 units). Metronome prices it:

    SUM(provider_cost_nanodollars)  x  (MARKUP x 1e-7)  cents per nanodollar

because 1 nanodollar = 1e-7 cents and the rate card is denominated in USD cents. At the owner's
1.2 markup the price is 1.2e-7 cents per nanodollar, so $0.001 of provider cost (1,000,000
units) debits 0.12 cents = $0.0012. A sandbox run proved Metronome keeps a 1e-7 cents/unit rate
exact (docs/runbooks/openrouter-managed-inference.md), so no unit conversion or rounding is
needed on the Metronome side.

MARKUP is read from the repo-root ``openrouter-pricing.json`` — the same file CDK feeds to the
gate's reservation and the settlement's fast-gate debit. The rate is ADDED to the env's
existing card (the one every contract already references); the 12 Bedrock rates are untouched.

Dry-run by default (reads only). ``--apply`` creates whatever is missing. Re-running is a no-op
once the metric, product and rate exist at the configured markup. A changed markup adds a new
rate segment from ``--starting-at`` (pass a future UTC midnight).

    cd apps/backend
    AWS_PROFILE=isol8-admin uv run python -m scripts.setup_metronome_openrouter_pricing --env dev
    AWS_PROFILE=isol8-admin uv run python -m scripts.setup_metronome_openrouter_pricing --env dev --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

BASE_URL = "https://api.metronome.com/v1"  # the env is decided by the API key alone
# Mirrors METRONOME_RATE_CARD_ID in apps/infra/lib/stacks/service-stack.ts (a test pins it).
RATE_CARDS = {"dev": "99576f5a-37d2-4983-ba64-fea9bad96e08", "prod": "fdac2edd-f207-4f11-b65b-01fad6fbbdbc"}
EVENT_TYPE = "openrouter_inference"
COST_KEY = "provider_cost_nanodollars"
METRIC_NAME = "OpenRouter provider cost (nanodollars)"
PRODUCT_NAME = "Managed AI usage"
CENTS_PER_NANODOLLAR = Decimal("1e-7")
PRICING_FILE = Path(__file__).resolve().parents[3] / "openrouter-pricing.json"


def load_markup(path: Path = PRICING_FILE) -> Decimal:
    markup = Decimal(json.loads(path.read_text())["markup"])
    if not markup.is_finite() or markup < 1:
        raise ValueError(f"markup must be a decimal >= 1, got {markup}")
    return markup


def rate_price(markup: Decimal) -> Decimal:
    """Cents charged per nanodollar of provider cost."""
    return markup * CENTS_PER_NANODOLLAR


def _pages(client: httpx.Client, method: str, path: str, body: dict | None = None) -> list[dict]:
    items, cursor = [], None
    while True:
        params = {"limit": 100, **({"next_page": cursor} if cursor else {})}
        resp = client.request(method, path, params=params, json=body)
        resp.raise_for_status()
        page = resp.json()
        items.extend(page.get("data") or [])
        cursor = page.get("next_page")
        if not cursor:
            return items


def _post(client: httpx.Client, path: str, body: dict) -> dict:
    resp = client.post(path, json=body)
    resp.raise_for_status()
    return resp.json()["data"]


def find_metric(client: httpx.Client) -> dict | None:
    for metric in _pages(client, "GET", "/billable-metrics"):
        if (
            str(metric.get("aggregation_type", "")).upper() == "SUM"
            and metric.get("aggregation_key") == COST_KEY
            and EVENT_TYPE in ((metric.get("event_type_filter") or {}).get("in_values") or [])
            and not metric.get("group_keys")
        ):
            return metric
    return None


def find_product(client: httpx.Client, metric_id: str) -> dict | None:
    for product in _pages(client, "POST", "/contract-pricing/products/list", {"archive_filter": "NOT_ARCHIVED"}):
        current = product.get("current") or {}
        if product.get("type") == "USAGE" and current.get("billable_metric_id") == metric_id:
            return product
    return None


def card_rates(client: httpx.Client, rate_card_id: str, at: str, product_id: str | None = None) -> list[dict]:
    body: dict = {"rate_card_id": rate_card_id, "at": at}
    if product_id:
        body["selectors"] = [{"product_id": product_id}]
    return _pages(client, "POST", "/contract-pricing/rate-cards/getRates", body)


def card_credit_type(client: httpx.Client, rate_card_id: str, at: str) -> str:
    """The card's one credit type (USD cents), read from its existing Bedrock rates."""
    types = {r["rate"]["credit_type"]["id"] for r in card_rates(client, rate_card_id, at)}
    if len(types) != 1:
        raise RuntimeError(f"rate card {rate_card_id} has {len(types)} credit types; expected exactly one")
    return types.pop()


def setup(
    client: httpx.Client, *, rate_card_id: str, markup: Decimal, starting_at: str, apply: bool, log=print
) -> dict:
    price = rate_price(markup)
    log(f"rate card {rate_card_id}; markup {markup} -> {price:f} cents per nanodollar from {starting_at}")
    out: dict = {"rate_card_id": rate_card_id, "price_cents_per_nanodollar": f"{price:f}"}

    metric = find_metric(client)
    if metric:
        out["metric_id"] = metric["id"]
        log(f"metric exists: {metric['id']}")
    else:
        log(f"metric missing: would create SUM({COST_KEY}) where event_type={EVENT_TYPE}")
        if apply:
            out["metric_id"] = _post(
                client,
                "/billable-metrics/create",
                {
                    "name": METRIC_NAME,
                    "event_type_filter": {"in_values": [EVENT_TYPE]},
                    "property_filters": [{"name": COST_KEY, "exists": True}],
                    "aggregation_type": "SUM",
                    "aggregation_key": COST_KEY,
                },
            )["id"]
            log(f"metric created: {out['metric_id']}")

    product = find_product(client, out["metric_id"]) if "metric_id" in out else None
    if product:
        out["product_id"] = product["id"]
        log(f"product exists: {product['id']}")
    else:
        log(f"product missing: would create USAGE product '{PRODUCT_NAME}' on the metric")
        if apply:
            out["product_id"] = _post(
                client,
                "/contract-pricing/products/create",
                {
                    "name": PRODUCT_NAME,
                    "type": "USAGE",
                    "billable_metric_id": out["metric_id"],
                },
            )["id"]
            log(f"product created: {out['product_id']}")

    existing = card_rates(client, rate_card_id, starting_at, out["product_id"]) if "product_id" in out else []
    if any(r.get("entitled") and Decimal(str(r["rate"]["price"])) == price for r in existing):
        log("rate exists at this markup: nothing to do")
        out["rate"] = "exists"
        return out
    log(f"rate missing: would add FLAT {price:f} cents/nanodollar (current: {[r['rate']['price'] for r in existing]})")
    if apply:
        _post(
            client,
            "/contract-pricing/rate-cards/addRate",
            {
                "rate_card_id": rate_card_id,
                "product_id": out["product_id"],
                "starting_at": starting_at,
                "entitled": True,
                "rate_type": "FLAT",
                "price": float(price),
                "credit_type_id": card_credit_type(client, rate_card_id, starting_at),
            },
        )
        out["rate"] = "added"
        log("rate added")
    else:
        out["rate"] = "missing"
    return out


def _api_key(env: str) -> str:
    import boto3

    return boto3.client("secretsmanager", region_name="us-east-1").get_secret_value(
        SecretId=f"lightbulb/{env}/metronome_api_key"
    )["SecretString"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", choices=sorted(RATE_CARDS), required=True)
    parser.add_argument("--apply", action="store_true", help="create what is missing (default: dry run)")
    parser.add_argument(
        "--starting-at",
        default=datetime.now(UTC).strftime("%Y-%m-%dT00:00:00Z"),
        help="rate start, a UTC midnight (default: today)",
    )
    args = parser.parse_args(argv)
    headers = {"Authorization": f"Bearer {_api_key(args.env)}"}
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30) as client:
        out = setup(
            client,
            rate_card_id=RATE_CARDS[args.env],
            markup=load_markup(),
            starting_at=args.starting_at,
            apply=args.apply,
        )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
