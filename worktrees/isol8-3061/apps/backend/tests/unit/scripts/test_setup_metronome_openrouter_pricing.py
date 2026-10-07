"""The OpenRouter Metronome setup script against a fake Metronome (httpx.MockTransport)."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from scripts import setup_metronome_openrouter_pricing as setup_script

CARD = "card-1"
AT = "2026-09-23T00:00:00Z"
BEDROCK_RATE = {
    "product_id": "bedrock",
    "entitled": True,
    "rate": {"price": 0.0003399, "credit_type": {"id": "usd-cents"}},
}


class FakeMetronome:
    def __init__(self, metrics=(), products=(), rates=()):
        self.metrics, self.products, self.rates = list(metrics), list(products), [BEDROCK_RATE, *rates]
        self.writes: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        body = json.loads(request.content) if request.content else {}
        if path == "/billable-metrics":
            return httpx.Response(200, json={"data": self.metrics, "next_page": None})
        if path == "/contract-pricing/products/list":
            return httpx.Response(200, json={"data": self.products, "next_page": None})
        if path == "/contract-pricing/rate-cards/getRates":
            wanted = {s["product_id"] for s in body.get("selectors", [])}
            rows = [r for r in self.rates if not wanted or r["product_id"] in wanted]
            return httpx.Response(200, json={"data": rows, "next_page": None})
        self.writes.append((path, body))
        if path == "/billable-metrics/create":
            self.metrics.append({"id": "metric-1", **body})
            return httpx.Response(200, json={"data": {"id": "metric-1"}})
        if path == "/contract-pricing/products/create":
            self.products.append(
                {"id": "product-1", "type": "USAGE", "current": {"billable_metric_id": body["billable_metric_id"]}}
            )
            return httpx.Response(200, json={"data": {"id": "product-1"}})
        if path == "/contract-pricing/rate-cards/addRate":
            self.rates.append(
                {
                    "product_id": body["product_id"],
                    "entitled": True,
                    "rate": {"price": body["price"], "credit_type": {"id": body["credit_type_id"]}},
                }
            )
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404)


def run(fake: FakeMetronome, *, apply: bool, markup: str = "1.2") -> dict:
    client = httpx.Client(base_url="https://api.metronome.com/v1", transport=httpx.MockTransport(fake))
    return setup_script.setup(
        client, rate_card_id=CARD, markup=Decimal(markup), starting_at=AT, apply=apply, log=lambda _: None
    )


def test_unit_math_one_million_nanodollars_is_point_one_two_cents():
    price = setup_script.rate_price(Decimal("1.2"))
    assert price == Decimal("1.2E-7")
    assert Decimal(1_000_000) * price == Decimal("0.12")  # $0.001 cost -> $0.0012 charged


def test_markup_comes_from_the_repo_file_and_is_the_owner_ruling():
    assert setup_script.load_markup() == Decimal("1.2")


def test_dry_run_writes_nothing():
    fake = FakeMetronome()
    out = run(fake, apply=False)
    assert fake.writes == []
    assert out["rate"] == "missing" and "metric_id" not in out


def test_apply_creates_metric_product_and_rate_then_is_idempotent():
    fake = FakeMetronome()
    out = run(fake, apply=True)
    assert [path for path, _ in fake.writes] == [
        "/billable-metrics/create",
        "/contract-pricing/products/create",
        "/contract-pricing/rate-cards/addRate",
    ]
    metric = fake.writes[0][1]
    assert metric["aggregation_type"] == "SUM" and metric["aggregation_key"] == "provider_cost_nanodollars"
    assert metric["event_type_filter"] == {"in_values": ["openrouter_inference"]}
    rate = fake.writes[2][1]
    assert rate["price"] == 1.2e-07 and rate["rate_type"] == "FLAT" and rate["entitled"] is True
    assert rate["credit_type_id"] == "usd-cents" and rate["rate_card_id"] == CARD and rate["starting_at"] == AT
    assert out == {
        "rate_card_id": CARD,
        "price_cents_per_nanodollar": "0.00000012",
        "metric_id": "metric-1",
        "product_id": "product-1",
        "rate": "added",
    }

    fake.writes.clear()
    assert run(fake, apply=True)["rate"] == "exists"
    assert fake.writes == []


def test_changed_markup_adds_a_new_rate_segment():
    fake = FakeMetronome()
    run(fake, apply=True)
    fake.writes.clear()
    run(fake, apply=True, markup="1.3")
    assert [(path, body["price"]) for path, body in fake.writes] == [("/contract-pricing/rate-cards/addRate", 1.3e-07)]


def test_markup_below_one_is_refused(tmp_path: Path):
    bad = tmp_path / "pricing.json"
    bad.write_text('{"markup": "0.9"}')
    with pytest.raises(ValueError):
        setup_script.load_markup(bad)


def test_rate_card_ids_match_the_deployed_stack():
    stack = (Path(setup_script.__file__).resolve().parents[2] / "infra/lib/stacks/service-stack.ts").read_text()
    block = stack[stack.index("METRONOME_RATE_CARD_ID:") :][:300]
    assert re.findall(r'"([0-9a-f-]{36})"', block) == [setup_script.RATE_CARDS["dev"], setup_script.RATE_CARDS["prod"]]
