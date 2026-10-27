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
    assert options["stripe_version"] == "2026-09-30.preview"
    assert body is None


@pytest.mark.asyncio
async def test_query_params_are_encoded_into_the_url_not_the_body():
    """A V2 GET carries its filters in the URL; encoding them ourselves keeps
    the transport swappable and the booleans explicit (`livemode=false`, not
    Python's `False`)."""
    raw = MagicMock(return_value={"object": "v2.list", "data": []})
    with patch.object(mod, "_raw", raw):
        await mod.paginate("acct_1", "/v2/provisioning/resources", params={"project": "proj_1", "limit": 100})
        await mod.payment_profile("acct_1")
    assert "project=proj_1" in raw.call_args_list[0].args[1]
    # sk_test_platform (the fixture key) is test mode.
    assert raw.call_args_list[1].args[1] == "/v2/provisioning/payment_profile?livemode=false"


@pytest.mark.asyncio
async def test_reveal_access_configuration_is_a_bodyless_post():
    raw = MagicMock(return_value={"resource": "res_1", "configuration": {"K": "v"}})
    with patch.object(mod, "_raw", raw):
        out = await mod.reveal_access_configuration("acct_1", "res_1")
    method, url, _options, body = raw.call_args.args
    assert (method, url) == ("post", "/v2/provisioning/resources/res_1/reveal_access_configuration")
    assert body is None
    assert out["configuration"] == {"K": "v"}


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
                "acct_1",
                service_ref="svc",
                provider="prvdr",
                project="proj",
                name="n",
                configuration={},
                livemode=False,
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
    revealed = {"resource": "res_1", "configuration": {"K": "sk_live_LEAK"}}
    with patch.object(mod, "_raw", MagicMock(return_value=revealed)), caplog.at_level("DEBUG"):
        await mod.reveal_access_configuration("acct_1", "res_1")
    # A revealed credential is the most sensitive response this API returns, so
    # the contract is stronger than "no secret in the text": a success logs
    # nothing at all, which is the only version of this that cannot rot.
    assert caplog.records == []


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
            livemode=False,
        )
    _m, url, _o, body = raw.call_args.args
    assert url == "/v2/provisioning/resources"
    # `livemode` is always on the wire: Stripe's default for an omitted one is
    # TRUE, i.e. real infrastructure and real charges.
    assert body == {
        "service_ref": "svc_1",
        "provider": "prvdr_1",
        "project": "proj_1",
        "name": "analytics",
        "configuration": {"region": "us"},
        "livemode": False,
    }


def test_create_resource_cannot_omit_livemode():
    import inspect

    assert inspect.signature(mod.create_resource).parameters["livemode"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_the_catalog_partition_is_configuration_on_every_catalog_read(monkeypatch):
    """dev reads `testing`, prod reads `prod`. Never a hardcoded literal."""
    monkeypatch.setattr(mod.settings, "STRIPE_PROJECTS_CATALOG", "testing")
    raw = MagicMock(return_value={"object": "v2.list", "data": []})
    with patch.object(mod, "_raw", raw):
        await mod.catalog_services("acct_1", provider_name="posthog")
        await mod.catalog_providers("acct_1")
        await mod.create_resource(
            "acct_1",
            service_ref="s",
            provider="p",
            project="proj_1",
            name="n",
            configuration={},
            livemode=False,
            catalog="testing",
        )
    # Each catalog read filters by the configured partition first; because the
    # fake returns no rows, the unfiltered fallback follows each filtered read
    # (see _catalog_with_fallback), so the calls pair up.
    assert "catalog=testing" in raw.call_args_list[0].args[1]
    assert "catalog=" not in raw.call_args_list[1].args[1]
    assert "catalog=testing" in raw.call_args_list[2].args[1]
    assert "catalog=" not in raw.call_args_list[3].args[1]
    assert raw.call_args_list[4].args[3]["catalog"] == "testing"


@pytest.mark.asyncio
async def test_provider_connection_request_names_its_project():
    raw = MagicMock(return_value={"id": "pcr_1", "status": "complete"})
    with patch.object(mod, "_raw", raw):
        await mod.create_provider_connection_request("acct_1", provider="prvdr_1", project="proj_1")
    _m, url, _o, body = raw.call_args.args
    assert url == "/v2/provisioning/provider_connection_requests"
    assert body == {"provider": "prvdr_1", "configuration": {}, "project": "proj_1"}


def test_the_retired_provisioning_paths_are_gone():
    """Contract guard: these paths 404 (or were renamed) on 2026-09-30.preview.
    The only module that speaks /v2/provisioning must not mention them, and the
    Accounts v2 calls live in company_stripe, never here."""
    import pathlib

    root = pathlib.Path(mod.__file__).resolve().parents[2]
    callers = [root / "core/services/stripe_provisioning.py", root / "core/services/provisioning_workflow.py"]
    callers.append(root / "routers/company_provisioning.py")
    for path in callers:
        source = path.read_text()
        for dead in ("account_requests", "environment_info", "/accounts", "account_links", "create_account_request"):
            assert dead not in source, f"{path.name} still mentions {dead}"


def test_teardown_cannot_reveal_credentials():
    """`remove` is the owner route's alone (test_wipe_never_deprovisions); the
    credential reveal is the workflow's bind step's alone. Teardown reaches neither."""
    import pathlib

    root = pathlib.Path(mod.__file__).resolve().parents[2]
    for name in ("account_wipe", "workspace_wipe"):
        assert "reveal_access_configuration" not in (root / f"core/services/{name}.py").read_text()
    users = sorted(
        str(p.relative_to(root))
        for d in ("core", "routers", "scripts")
        for p in (root / d).rglob("*.py")
        if "reveal_access_configuration(" in p.read_text() and p.name != "stripe_provisioning.py"
    )
    assert users == ["core/services/provisioning_workflow.py"], users


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
    assert set(body) == {"livemode", "usage_limits"}  # no payment_method_owner, no source_*
    assert body["livemode"] is False
    assert out["checkout_session_url"] == "https://c"


@pytest.mark.asyncio
async def test_max_amount_goes_over_the_wire_as_a_string():
    """WIRE FACT: Stripe rejects an integer with "Potentially large numbers
    must be String values for safety"."""
    raw = MagicMock(return_value={"status": "checkout_initiated", "checkout_session_url": "https://c"})
    with patch.object(mod, "_raw", raw):
        await mod.create_payment_method_request("acct_1", max_amount=1000, currency="usd", recurring_interval="month")
    limits = raw.call_args_list[0].args[3]["usage_limits"]
    assert limits["max_amount"] == "1000" and isinstance(limits["max_amount"], str)


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


@pytest.mark.asyncio
async def test_a_non_object_error_body_is_typed_and_logged_not_a_crash():
    """An intermediary (WAF, proxy, load balancer) answers HTML, and the SDK
    raises APIError with that raw body as `json_body`. Reaching into it as a
    dict would raise AttributeError out of `request` BEFORE the log line — the
    one failure mode that is neither typed nor recorded."""
    err = mod.stripe.error.APIError("Invalid response object from API: '<html>502</html>'")
    err.json_body = "<html>502 Bad Gateway</html>"
    err.http_status = 502
    logged = MagicMock()
    with patch.object(mod, "_raw", MagicMock(side_effect=err)), patch.object(mod, "logger", logged):
        with pytest.raises(mod.ProvisioningError) as exc:
            await mod.get_resource("acct_1", "res_x")
    assert exc.value.code == "provisioning_error"
    assert logged.warning.call_count == 1
    # Path and code only — never the body the SDK folded into its message.
    assert "<html>" not in repr(logged.mock_calls)


@pytest.mark.asyncio
async def test_a_message_carrying_a_whole_response_body_is_bounded():
    err = mod.stripe.error.APIError("Invalid response object from API: " + "x" * 5000)
    err.json_body = ["not", "an", "object"]
    with patch.object(mod, "_raw", MagicMock(side_effect=err)):
        with pytest.raises(mod.ProvisioningError) as exc:
            await mod.eligibility("acct_1")
    assert len(exc.value.message) == mod._MAX_MESSAGE_CHARS


@pytest.mark.asyncio
async def test_a_429_is_transient_and_carries_retry_after():
    err = mod.stripe.error.RateLimitError("slow down")
    err.http_status = 429
    err.headers = {"retry-after": "2.5"}
    with patch.object(mod, "_raw", MagicMock(side_effect=err)):
        with pytest.raises(mod.TransientProvisioningError) as exc:
            await mod.eligibility("acct_1")
    assert exc.value.http_status == 429
    assert exc.value.retry_after == 2.5


@pytest.mark.asyncio
async def test_a_missing_or_dated_retry_after_is_none_not_a_crash():
    err = mod.stripe.error.InvalidRequestError("nope", None, code="not_found")
    err.http_status = 404
    err.headers = {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}
    with patch.object(mod, "_raw", MagicMock(side_effect=err)):
        with pytest.raises(mod.ProvisioningError) as exc:
            await mod.get_resource("acct_1", "res_x")
    assert exc.value.retry_after is None
    assert not isinstance(exc.value, mod.TransientProvisioningError)


def test_the_stripe_client_is_built_with_a_network_timeout():
    """`asyncio.wait_for` frees the caller but cannot kill the worker thread,
    so the bound that actually cuts a hung read is the socket timeout. The
    SDK's default is 80s."""
    made = MagicMock()
    with patch.object(mod, "StripeClient", made):
        mod._client()
    assert made.call_args.kwargs["http_client"]._timeout == mod.TIMEOUT_S


@pytest.mark.asyncio
async def test_a_get_refuses_a_body_instead_of_folding_it_into_the_query():
    with patch.object(mod, "_raw", MagicMock()) as raw:
        with pytest.raises(ValueError):
            await mod.request("acct_1", "get", "/v2/provisioning/resources", body={"project": "proj_1"})
    raw.assert_not_called()


@pytest.mark.asyncio
async def test_a_paginated_failure_logs_the_bare_path_not_the_page_token():
    """`paginate` hands `request` a url that already carries the opaque page
    value; the log must not become a place page tokens accumulate."""
    err = mod.stripe.error.InvalidRequestError("nope", None, code="not_found")
    logged = MagicMock()
    with patch.object(mod, "_raw", MagicMock(side_effect=err)), patch.object(mod, "logger", logged):
        with pytest.raises(mod.ProvisioningError):
            await mod.paginate("acct_1", "/v2/provisioning/resources", params={"limit": 100})
    emitted = repr(logged.mock_calls)
    assert "/v2/provisioning/resources" in emitted
    assert "limit=100" not in emitted and "?" not in emitted


@pytest.mark.asyncio
async def test_charges_passes_limit_and_page_and_is_not_a_v2_list():
    raw = MagicMock(
        return_value={"usage": [], "next_page_url": "https://api.stripe.com/v2/provisioning/charges?page=P"}
    )
    with patch.object(mod, "_raw", raw):
        out = await mod.charges("acct_1", start_time="t0", end_time="t1", livemode=True, limit=50, page="P")
    url = raw.call_args.args[1]
    assert "limit=50" in url and "page=P" in url and "livemode=true" in url
    # `usage`, not `data` — which is why paginate() is not used here.
    assert "data" not in out


@pytest.mark.asyncio
async def test_charges_usage_walks_usage_rows_that_paginate_would_miss():
    pages = [
        {"usage": [{"id": "u1"}], "next_page_url": "https://api.stripe.com/v2/provisioning/charges?page=OPAQUE"},
        {"usage": [{"id": "u2"}]},
    ]
    raw = MagicMock(side_effect=pages)
    with patch.object(mod, "_raw", raw):
        rows, truncated = await mod.charges_usage("acct_1", start_time="t0", end_time="t1", livemode=True)
    assert [r["id"] for r in rows] == ["u1", "u2"]
    assert truncated is False
    # The RETURNED url verbatim (path + query), never a page value we built.
    assert raw.call_args_list[1].args[1].endswith("/v2/provisioning/charges?page=OPAQUE")


@pytest.mark.asyncio
async def test_paginate_collects_nothing_from_a_usage_response():
    """Why charges_usage exists: paginate reads `data`, /charges answers `usage`."""
    with patch.object(mod, "_raw", MagicMock(return_value={"usage": [{"id": "u1"}]})):
        assert await mod.paginate("acct_1", "/v2/provisioning/charges") == []


@pytest.mark.asyncio
async def test_charges_usage_reports_a_walk_it_had_to_cut_short():
    endless = {"usage": [{"id": "u"}], "next_page_url": "https://api.stripe.com/v2/provisioning/charges?page=P"}
    with patch.object(mod, "_raw", MagicMock(return_value=endless)):
        rows, truncated = await mod.charges_usage(
            "acct_1", start_time="t0", end_time="t1", livemode=True, limit_pages=3
        )
    assert len(rows) == 3
    assert truncated is True


def test_livemode_follows_the_platform_key_not_the_catalog(monkeypatch):
    monkeypatch.setattr(mod.settings, "STRIPE_PROJECTS_CATALOG", "prod")
    monkeypatch.setattr(mod.settings, "STRIPE_SECRET_KEY", "sk_test_sandbox")
    assert mod.is_livemode() is False
    monkeypatch.setattr(mod.settings, "STRIPE_SECRET_KEY", "sk_live_platform")
    assert mod.is_livemode() is True


@pytest.mark.asyncio
async def test_deep_link_can_select_the_catalog_partition():
    raw = MagicMock(return_value={"purpose": "dashboard", "url": "https://p", "expires_time": "t"})
    with patch.object(mod, "_raw", raw):
        await mod.create_deep_link("acct_1", provider="prvdr_1", catalog="testing")
    assert raw.call_args.args[3]["catalog"] == "testing"


@pytest.mark.asyncio
async def test_request_unwraps_stripe_response_body():
    """StripeClient.raw_request returns a StripeResponse, not a dict — `.data`
    is the ALREADY-PARSED body (json.loads with OrderedDict pairs), `.body` the
    raw text. Built on the real SDK class so the contract cannot drift: two
    hand-modeled fakes in a row asserted the wrong shape and 500'd dev."""

    from stripe import StripeResponse

    real = StripeResponse('{"is_eligible": true, "requirements": []}', 200, {})

    with patch.object(mod, "_raw", return_value=real):
        out = await mod.request("acct_x", "get", "/v2/provisioning/eligibility")
    assert out == {"is_eligible": True, "requirements": []}


@pytest.mark.asyncio
async def test_catalog_falls_back_when_partition_filter_matches_nothing():
    """The sandbox returns services with catalog:null (spec: 'a Preview
    implementation field'), so filtering by partition can blank the catalog.
    Empty filtered read -> retry unfiltered."""
    calls = []

    async def fake_paginate(account_id, path, *, params=None, limit_pages=20):
        calls.append(dict(params))
        return [] if params.get("catalog") else [{"id": "prvsvc_1"}]

    with patch.object(mod, "paginate", side_effect=fake_paginate):
        rows = await mod.catalog_services("acct_x")
    assert rows == [{"id": "prvsvc_1"}]
    assert calls[0].get("catalog") and "catalog" not in calls[1]


@pytest.mark.asyncio
async def test_catalog_no_fallback_when_filtered_read_has_rows():
    async def fake_paginate(account_id, path, *, params=None, limit_pages=20):
        return [{"id": "prvsvc_filtered"}]

    with patch.object(mod, "paginate", side_effect=fake_paginate) as pg:
        rows = await mod.catalog_services("acct_x")
    assert rows == [{"id": "prvsvc_filtered"}]
    assert pg.call_count == 1
