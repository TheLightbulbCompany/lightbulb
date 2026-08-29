"""The client's contract: headers, timeout, pagination, error mapping, redaction.

`_raw` (the one blocking call) is patched, so nothing here touches the network
and the tests are identical whether `_raw` wraps the Stripe SDK or httpx.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import stripe_provisioning as mod  # noqa: E402


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(mod.settings, "STRIPE_SECRET_KEY", "sk_test_platform")


@pytest.mark.asyncio
async def test_request_sends_context_and_preview_version():
    raw = MagicMock(return_value={"is_eligible": True, "requirements": []})
    with patch.object(mod, "_raw", raw):
        out = await mod.eligibility("acct_1")
    assert out == {"is_eligible": True, "requirements": []}
    method, url, options, body = raw.call_args.args
    assert (method, url) == ("get", "/v2/provisioning/eligibility")
    assert options["stripe_context"] == "acct_1"
    assert options["stripe_version"] == "unsafe-development"
    assert body is None


@pytest.mark.asyncio
async def test_query_params_are_encoded_into_the_url_not_the_body():
    """A V2 GET carries its filters in the URL; encoding them ourselves keeps
    the transport swappable and the `reveal`/`scope` booleans explicit."""
    raw = MagicMock(return_value={"resource_access_configurations": []})
    with patch.object(mod, "_raw", raw):
        await mod.environment_info("acct_1", project="proj_1", provider="prvdr_1", service="svc_1", reveal=True)
    _method, url, _options, _body = raw.call_args.args
    assert url.startswith("/v2/provisioning/environment_info?")
    assert "reveal=true" in url and "scope=SERVICE" in url
    assert "project=proj_1" in url and "provider=prvdr_1" in url and "service=svc_1" in url


@pytest.mark.asyncio
async def test_paginate_follows_next_page_url_and_never_builds_a_page_value():
    pages = [
        {
            "object": "v2.list",
            "data": [{"id": "a"}],
            "next_page_url": "https://api.stripe.com/v2/provisioning/x?page=OPAQUE",
        },
        {"object": "v2.list", "data": [{"id": "b"}]},
    ]
    raw = MagicMock(side_effect=pages)
    with patch.object(mod, "_raw", raw):
        out = await mod.paginate("acct_1", "/v2/provisioning/x", params={"limit": 100})
    assert [r["id"] for r in out] == ["a", "b"]
    # Second call uses the RETURNED url verbatim (path + query), never a page we built.
    second_url = raw.call_args_list[1].args[1]
    assert second_url.endswith("/v2/provisioning/x?page=OPAQUE")


@pytest.mark.asyncio
async def test_paginate_stops_at_the_page_bound():
    endless = {
        "object": "v2.list",
        "data": [{"id": "a"}],
        "next_page_url": "https://api.stripe.com/v2/provisioning/x?page=P",
    }
    with patch.object(mod, "_raw", MagicMock(return_value=endless)):
        out = await mod.paginate("acct_1", "/v2/provisioning/x", limit_pages=3)
    assert len(out) == 3


@pytest.mark.asyncio
async def test_stripe_error_becomes_a_typed_provisioning_error():
    err = mod.stripe.error.InvalidRequestError("Payment profile required", None, code="payment_method_required")
    err.request_id = "req_123"
    err.http_status = 400
    with patch.object(mod, "_raw", MagicMock(side_effect=err)):
        with pytest.raises(mod.ProvisioningError) as exc:
            await mod.create_resource(
                "acct_1", service_ref="svc", provider="prvdr", project="proj", name="n", configuration={}
            )
    assert exc.value.code == "payment_method_required"
    assert exc.value.request_id == "req_123"
    assert exc.value.http_status == 400


@pytest.mark.asyncio
async def test_error_code_is_read_from_the_v2_body_when_the_attribute_is_empty():
    err = mod.stripe.error.InvalidRequestError("nope", None)
    err.json_body = {"error": {"type": "invalid_request_error", "code": "not_found"}}
    with patch.object(mod, "_raw", MagicMock(side_effect=err)):
        with pytest.raises(mod.ProvisioningError) as exc:
            await mod.get_resource("acct_1", "res_x")
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_unknown_failures_get_a_stable_code_not_a_raw_message():
    with patch.object(mod, "_raw", MagicMock(side_effect=TimeoutError("read timeout"))):
        with pytest.raises(mod.ProvisioningError) as exc:
            await mod.get_resource("acct_1", "res_x")
    assert exc.value.code == "transport_error"


@pytest.mark.asyncio
async def test_logs_carry_no_secret_bearing_field(caplog):
    revealed = {
        "resource_access_configurations": [
            {"resource_id": "res_1", "access_configuration_keys": ["K"], "access_configuration": {"K": "sk_live_LEAK"}}
        ]
    }
    with patch.object(mod, "_raw", MagicMock(return_value=revealed)), caplog.at_level("DEBUG"):
        await mod.environment_info("acct_1", project="proj_1", provider="prvdr_1", service="svc_1", reveal=True)
    assert "sk_live_LEAK" not in caplog.text
    assert "access_configuration" not in caplog.text


@pytest.mark.asyncio
async def test_a_failing_response_body_never_reaches_the_logger():
    """The failure path is the one that gets logged, so it is the one that can
    leak. Stripe's own error envelope carries the request body back."""
    err = mod.stripe.error.InvalidRequestError("nope", None, code="invalid_resource_configuration")
    err.json_body = {
        "error": {"code": "invalid_resource_configuration", "message": "https://checkout.stripe.com/LEAK"},
        "checkout_session_url": "https://checkout.stripe.com/LEAK",
        "access_configuration": {"K": "sk_live_LEAK"},
    }
    logged = MagicMock()
    with patch.object(mod, "_raw", MagicMock(side_effect=err)), patch.object(mod, "logger", logged):
        with pytest.raises(mod.ProvisioningError):
            await mod.create_payment_method_request(
                "acct_1", max_amount=1000, currency="usd", recurring_interval="month"
            )
    emitted = repr(logged.mock_calls)
    assert logged.mock_calls, "the failure must be logged at all"
    for leak in ("LEAK", "checkout_session_url", "access_configuration", "usage_limits"):
        assert leak not in emitted


def test_redact_strips_every_secret_bearing_key():
    """The names come from spec.txt §6 Security requirements."""
    body = {
        "access_configuration": {"K": "v"},
        "value": "v",
        "key": "v",
        "confirmation_secret": "v",
        "code_verifier": "v",
        "checkout_session_url": "https://checkout",
        "url": "https://deeplink",
        "redirect_url": "https://provider",
        "id": "res_1",
        "status": "complete",
    }
    out = mod.redact(body)
    assert out["id"] == "res_1" and out["status"] == "complete"
    for k in (
        "access_configuration",
        "value",
        "key",
        "confirmation_secret",
        "code_verifier",
        "checkout_session_url",
        "url",
        "redirect_url",
    ):
        assert out[k] == "[redacted]"


@pytest.mark.asyncio
async def test_create_resource_sends_the_current_field_names():
    raw = MagicMock(return_value={"id": "res_1", "status": "pending"})
    with patch.object(mod, "_raw", raw):
        await mod.create_resource(
            "acct_1",
            service_ref="svc_1",
            provider="prvdr_1",
            project="proj_1",
            name="analytics",
            configuration={"region": "us"},
        )
    _m, url, _o, body = raw.call_args.args
    assert url == "/v2/provisioning/resources"
    # `project`, not the deprecated `project_id` (spec.txt 4.4 + guide's
    # "current exception is Account request project_id").
    assert body == {
        "service_ref": "svc_1",
        "provider": "prvdr_1",
        "project": "proj_1",
        "name": "analytics",
        "configuration": {"region": "us"},
    }


@pytest.mark.asyncio
async def test_the_catalog_partition_is_configuration_on_every_catalog_read(monkeypatch):
    """dev reads `testing`, prod reads `prod`. Never a hardcoded literal."""
    monkeypatch.setattr(mod.settings, "STRIPE_PROJECTS_CATALOG", "testing")
    raw = MagicMock(return_value={"object": "v2.list", "data": []})
    with patch.object(mod, "_raw", raw):
        await mod.catalog_services("acct_1", provider_name="posthog")
        await mod.catalog_providers("acct_1")
        await mod.create_resource(
            "acct_1", service_ref="s", provider="p", project="proj_1", name="n", configuration={}, catalog="testing"
        )
    assert "catalog=testing" in raw.call_args_list[0].args[1]
    assert "catalog=testing" in raw.call_args_list[1].args[1]
    assert raw.call_args_list[2].args[3]["catalog"] == "testing"


@pytest.mark.asyncio
async def test_account_request_uses_project_id_the_documented_exception():
    raw = MagicMock(return_value={"id": "ar_1", "status": "requested"})
    with patch.object(mod, "_raw", raw):
        await mod.create_account_request("acct_1", provider="prvdr_1", project_id="proj_1")
    _m, url, _o, body = raw.call_args.args
    assert url == "/v2/provisioning/account_requests"
    assert body == {"provider": "prvdr_1", "configuration": {}, "project_id": "proj_1"}


@pytest.mark.asyncio
async def test_payment_method_request_is_hosted_collection_with_no_source_fields():
    """Verified 2026-08-29 against our live platform key: hosted collection
    under Stripe-Context returns checkout_initiated + a checkout URL."""
    raw = MagicMock(return_value={"status": "checkout_initiated", "checkout_session_url": "https://c"})
    with patch.object(mod, "_raw", raw):
        out = await mod.create_payment_method_request(
            "acct_1", max_amount=1000, currency="usd", recurring_interval="month"
        )
    _m, url, _o, body = raw.call_args.args
    assert url == "/v2/provisioning/payment_method_requests"
    assert set(body) == {"usage_limits"}  # no payment_method_owner, no source_*
    assert out["checkout_session_url"] == "https://c"


@pytest.mark.asyncio
async def test_max_amount_goes_over_the_wire_as_a_string():
    """WIRE FACT: Stripe rejects an integer with "Potentially large numbers
    must be String values for safety"."""
    raw = MagicMock(return_value={"status": "checkout_initiated", "checkout_session_url": "https://c"})
    with patch.object(mod, "_raw", raw):
        await mod.create_payment_method_request("acct_1", max_amount=1000, currency="usd", recurring_interval="month")
        await mod.update_payment_limit(
            "acct_1", max_amount=2500, currency="usd", recurring_interval="month", provider="prvdr_1"
        )
    first = raw.call_args_list[0].args[3]["usage_limits"]
    second = raw.call_args_list[1].args[3]["usage_limits"]
    assert first["max_amount"] == "1000" and isinstance(first["max_amount"], str)
    assert second["max_amount"] == "2500" and isinstance(second["max_amount"], str)
    assert raw.call_args_list[1].args[3]["provider"] == "prvdr_1"


@pytest.mark.asyncio
async def test_a_stripe_5xx_is_transport_shaped_so_reads_can_retry():
    """Preview flakiness is real (features/identity returned 500 while
    eligibility worked). A 5xx must be retryable-by-READ, never by re-POST."""
    err = mod.stripe.error.APIError("api_error")
    err.http_status = 500
    with patch.object(mod, "_raw", MagicMock(side_effect=err)):
        with pytest.raises(mod.ProvisioningError) as exc:
            await mod.eligibility("acct_1")
    assert exc.value.http_status == 500
    # Typed so a caller retries by class, never by re-reading a status number.
    assert isinstance(exc.value, mod.TransientProvisioningError)


@pytest.mark.asyncio
async def test_a_transport_timeout_is_transient_too():
    """`request` bounds every call at TIMEOUT_S; a state-changing method that
    times out is `transport_error` — safe to re-READ, never to re-POST."""
    with patch.object(mod, "_raw", MagicMock(side_effect=TimeoutError())):
        with pytest.raises(mod.TransientProvisioningError) as exc:
            await mod.create_deep_link("acct_1", provider="prvdr_1")
    assert exc.value.code == "transport_error"
