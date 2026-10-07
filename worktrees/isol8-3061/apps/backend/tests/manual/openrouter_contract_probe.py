"""Read-only development billing preflight. Prints only selected status fields.

Run: python3 apps/backend/tests/manual/openrouter_contract_probe.py
Requires AWS SSO for isol8-admin and boto3. Never uses production secrets.
"""

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import boto3


REGION = "us-east-1"
PROFILE = "isol8-admin"
METRONOME_PRODUCT = "6dab1634-020d-4288-b5e6-99568a30404c"
METRONOME_RATE_CARD = "99576f5a-37d2-4983-ba64-fea9bad96e08"


def read_json(url, token, *, body=None):
    request = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, {}
    except URLError:
        return "network_error", {}


def main():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != "877352799272":
        raise SystemExit("Unexpected AWS account; dev readback stopped")
    secrets = session.client("secretsmanager")
    stripe_key = secrets.get_secret_value(SecretId="lightbulb/dev/stripe_secret_key")["SecretString"]
    metronome_key = secrets.get_secret_value(SecretId="lightbulb/dev/metronome_api_key")["SecretString"]
    if not stripe_key.startswith("sk_test_"):
        raise SystemExit("Stripe key is not test-mode; dev readback stopped")

    stripe_status, stripe = read_json("https://api.stripe.com/v1/account", stripe_key)
    provider_status, providers = read_json(
        "https://api.metronome.com/v1/listConfiguredBillingProviders", metronome_key, body={}
    )
    product_status, product = read_json(
        "https://api.metronome.com/v1/contract-pricing/products/get",
        metronome_key,
        body={"id": METRONOME_PRODUCT},
    )
    rate_status, rate = read_json(
        "https://api.metronome.com/v1/contract-pricing/rate-cards/get",
        metronome_key,
        body={"id": METRONOME_RATE_CARD},
    )
    stripe_id = stripe.get("id")
    integrations = [item for item in providers.get("data", []) if item.get("billing_provider") == "stripe"]
    linked = (
        [item.get("delivery_method_configuration", {}).get("stripe_account_id") == stripe_id for item in integrations]
        if stripe_id
        else []
    )
    stripe_product_id = product.get("data", {}).get("custom_fields", {}).get("stripe_product_id")
    mapped_status, mapped_product = (None, {})
    if stripe_product_id:
        mapped_status, mapped_product = read_json(
            f"https://api.stripe.com/v1/products/{quote(stripe_product_id, safe='')}", stripe_key
        )
    result = {
        "environment": "dev",
        "stripe_account_http": stripe_status,
        "stripe_key_test_mode": True,
        "metronome_providers_http": provider_status,
        "stripe_integration_count": len(integrations),
        "stripe_account_match_count": sum(linked),
        "metronome_product_http": product_status,
        "metronome_product_found": product.get("data", {}).get("id") == METRONOME_PRODUCT,
        "mapped_stripe_product_http": mapped_status,
        "mapped_stripe_product_active": mapped_product.get("active") is True,
        "metronome_rate_card_http": rate_status,
        "metronome_rate_card_found": rate.get("data", {}).get("id") == METRONOME_RATE_CARD,
    }
    if "--details" in sys.argv:
        result["dev_stripe_account_id"] = stripe_id
        result["metronome_stripe_integrations"] = [
            {
                "delivery_method_id": item.get("delivery_method_id"),
                "stripe_account_id": item.get("delivery_method_configuration", {}).get("stripe_account_id"),
                "leave_invoices_in_draft": item.get("delivery_method_configuration", {}).get("leave_invoices_in_draft"),
            }
            for item in integrations
        ]
        result["metronome_credit_product_id"] = METRONOME_PRODUCT
        result["mapped_stripe_product_id"] = stripe_product_id
        result["metronome_rate_card_id"] = METRONOME_RATE_CARD
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
