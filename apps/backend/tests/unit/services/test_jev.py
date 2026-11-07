"""Jev client + agent cards (spec 2026-09-22-jev-routing-proactive §3a, slice 1)."""

import json
import logging

import threading

import httpx
import pytest
import yaml
from unittest.mock import AsyncMock

from core.services import jev

KEY = "vck_super_secret_key_value"
_RealClient = httpx.AsyncClient
_REAL_READ_SECRET = jev._read_secret  # conftest stubs it for every other test


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(jev, "_read_secret", _REAL_READ_SECRET)
    monkeypatch.setattr(jev, "_key", None)
    monkeypatch.setattr(jev, "_key_checked_at", 0.0)
    monkeypatch.setattr(jev, "_cards", {})
    metrics: list[tuple[str, dict]] = []
    monkeypatch.setattr(jev, "put_metric", lambda name, **kw: metrics.append((name, kw)))
    return metrics


def _metrics(monkeypatch):
    out: list[tuple[str, dict]] = []
    monkeypatch.setattr(jev, "put_metric", lambda name, **kw: out.append((name, kw)))
    return out


def _gateway(monkeypatch, handler):
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value=KEY))
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        jev.httpx, "AsyncClient", lambda **kw: _RealClient(transport=httpx.MockTransport(wrapped), **kw)
    )
    return seen


ANSWERS = {"who": {"type": "choice", "choice": "a1", "probabilities": {"a1": 0.9, "none": 0.1}, "confidence": 0.9}}


@pytest.mark.asyncio
async def test_decide_ok_posts_zdr_request_and_meters_cost_tokens(monkeypatch):
    metrics = _metrics(monkeypatch)
    seen = _gateway(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "answers": ANSWERS,
                "usage": {"inputTokens": 740},
                "providerMetadata": {"gateway": {"cost": "0.0000311"}},
            },
        ),
    )
    assert await jev.decide({"s": 1}, {"who": {}}) == ANSWERS
    body = json.loads(seen[0].content)
    assert seen[0].url == "https://ai-gateway.vercel.sh/v1/evaluate"
    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == "typesafe-ai/jev"
    assert body["providerOptions"] == {"gateway": {"zeroDataRetention": True, "only": ["typesafe-ai"]}}
    names = {name: kw for name, kw in metrics}
    assert names["messaging.jev.decision"]["dimensions"] == {"outcome": "ok"}
    assert names["messaging.jev.cost"]["value"] == pytest.approx(0.0000311)
    assert names["messaging.jev.input_tokens"]["value"] == 740
    assert "messaging.jev.latency" in names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "outcome"),
    [
        (lambda r: httpx.Response(500, text="boom"), "error"),
        (lambda r: httpx.Response(200, json={"nope": 1}), "error"),
    ],
)
async def test_decide_failures_return_none(monkeypatch, handler, outcome):
    metrics = _metrics(monkeypatch)
    _gateway(monkeypatch, handler)
    assert await jev.decide({}, {}) is None
    assert ("messaging.jev.decision", {"dimensions": {"outcome": outcome}}) in metrics


@pytest.mark.asyncio
async def test_decide_403_logs_the_gateway_error_type_and_message_and_meters_forbidden(monkeypatch, caplog):
    """Prod 2026-09-27: the Vercel AI Gateway account ran out of credits and every call 403'd
    with no signal beyond "Jev returned 403" in the logs. The status code alone doesn't say
    whether the KEY, the MODEL, or the ACCOUNT is the problem -- the gateway's own error body
    does, so log it (never the key, never the request body) and meter it distinctly."""
    metrics = _metrics(monkeypatch)
    _gateway(
        monkeypatch,
        lambda r: httpx.Response(
            403,
            json={"error": {"type": "forbidden", "message": "Free tier users do not have access to this model"}},
        ),
    )
    with caplog.at_level(logging.WARNING):
        assert await jev.decide({}, {}) is None
    assert ("messaging.jev.decision", {"dimensions": {"outcome": "forbidden"}}) in metrics
    assert "403" in caplog.text and "forbidden" in caplog.text
    assert "Free tier users do not have access to this model" in caplog.text
    assert KEY not in caplog.text


@pytest.mark.asyncio
async def test_decide_rate_limited_returns_the_sentinel(monkeypatch):
    metrics = _metrics(monkeypatch)
    _gateway(monkeypatch, lambda r: httpx.Response(429))
    assert await jev.decide({}, {}) == jev.RATE_LIMITED
    assert ("messaging.jev.decision", {"dimensions": {"outcome": "rate_limited"}}) in metrics


@pytest.mark.asyncio
async def test_decide_timeout_returns_none(monkeypatch):
    metrics = _metrics(monkeypatch)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    _gateway(monkeypatch, slow)
    assert await jev.decide({}, {}) is None
    assert ("messaging.jev.decision", {"dimensions": {"outcome": "timeout"}}) in metrics


@pytest.mark.asyncio
async def test_missing_secret_disables_and_retries_only_after_backoff(monkeypatch, caplog):
    metrics = _metrics(monkeypatch)
    calls = []

    class Sm:
        def get_secret_value(self, SecretId):
            calls.append(SecretId)
            raise RuntimeError("ResourceNotFoundException")

    monkeypatch.setattr(jev.boto3, "client", lambda *a, **k: Sm())
    assert await jev.decide({}, {}) is None
    assert await jev.decide({}, {}) is None
    assert calls == ["lightbulb/dev/vercel-ai-gateway"] or calls[0].endswith("/vercel-ai-gateway")
    assert len(calls) == 1  # cached failure inside the 5-minute backoff
    assert metrics.count(("messaging.jev.decision", {"dimensions": {"outcome": "disabled"}})) == 2
    monkeypatch.setattr(jev, "_key_checked_at", jev._key_checked_at - 301)
    await jev.decide({}, {})
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_key_is_reread_after_an_hour_on_a_bounded_client_off_the_loop(monkeypatch):
    calls, configs = [], []

    class Sm:
        def get_secret_value(self, SecretId):
            calls.append(threading.current_thread().name)
            return {"SecretString": f"key-{len(calls)}"}

    def client(service, **kw):
        configs.append((service, kw["config"]))
        return Sm()

    monkeypatch.setattr(jev.boto3, "client", client)
    assert await jev._gateway_key() == "key-1"
    assert await jev._gateway_key() == "key-1"  # cached
    monkeypatch.setattr(jev, "_key_checked_at", jev._key_checked_at - 3601)
    assert await jev._gateway_key() == "key-2"  # rotation lands without a restart
    assert all(name.startswith("jev") for name in calls)
    service, config = configs[0]
    assert service == "secretsmanager"
    assert (config.connect_timeout, config.read_timeout, config.retries) == (2, 3, {"max_attempts": 1})


def test_profile_reads_use_a_bounded_s3_client(monkeypatch):
    seen = []

    class S3:
        def get_object(self, Bucket, Key):
            raise jev.botocore.exceptions.ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    monkeypatch.setattr(jev, "_s3_client", None)
    monkeypatch.setattr(jev.boto3, "client", lambda service, **kw: seen.append((service, kw["config"])) or S3())
    monkeypatch.setattr(jev.settings, "OWNER_STATE_BUCKET", "b")
    assert jev._read("org_1/hermes/a1/SOUL.md") is None
    service, config = seen[0]
    assert service == "s3"
    assert (config.connect_timeout, config.read_timeout, config.retries) == (2, 3, {"max_attempts": 1})


@pytest.mark.asyncio
async def test_card_reads_run_on_the_jev_executor(monkeypatch):
    threads = []

    def get_object(key):
        threads.append(threading.current_thread().name)
        return None

    monkeypatch.setattr(jev, "_read", get_object)
    monkeypatch.setattr(jev, "_registry_agent", AsyncMock(return_value={}))
    await jev.agent_card("org_1", "a1", "Bob")
    assert threads and all(name.startswith("jev") for name in threads)


@pytest.mark.asyncio
async def test_key_is_never_logged(monkeypatch, caplog):
    class Sm:
        def get_secret_value(self, SecretId):
            return {"SecretString": KEY}

    monkeypatch.setattr(jev.boto3, "client", lambda *a, **k: Sm())

    def fail(request):
        raise httpx.ConnectError(f"refused {request.headers['Authorization']}", request=request)

    monkeypatch.setattr(jev.httpx, "AsyncClient", lambda **kw: _RealClient(transport=httpx.MockTransport(fail), **kw))
    with caplog.at_level(logging.DEBUG):
        assert await jev.decide({}, {}) is None
        monkeypatch.setattr(
            jev.httpx,
            "AsyncClient",
            lambda **kw: _RealClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw),
        )
        assert await jev.decide({}, {}) is None
    assert KEY not in caplog.text


# --------------------------------------------------------------------------- cards


def _profile(monkeypatch, files: dict[str, str], config: dict, agent: dict | None = None):
    reads: list[str] = []

    files = {**files, "config.yaml": yaml.safe_dump(config)} if config else files

    def get_object(key):
        reads.append(key)
        name = key.rsplit("/", 1)[-1]
        return files[name].encode() if name in files else None

    monkeypatch.setattr(jev, "_read", get_object)
    paperclip = AsyncMock(return_value=agent or {})
    monkeypatch.setattr(jev, "_registry_agent", paperclip)
    return reads, paperclip


SOUL_WHAT = (
    "<!-- lightbulb:generated -->\n\n# Bob\n\n**Finance**\n\nYou are Bob, the cfo agent on Lightbulb.\n\n"
    "## What you do\n\nReconciles **Stripe** payouts. Chases overdue invoices.\n"
)


@pytest.mark.asyncio
async def test_card_uses_soul_what_you_do_over_role_fallback_and_role_not_name(monkeypatch):
    _profile(monkeypatch, {"SOUL.md": SOUL_WHAT, ".revision": "r1"}, {}, {"name": "Bob", "role": "cfo"})
    card = await jev.agent_card("org_1", "a1", "Bob")
    assert card.startswith("Bob (role: cfo). You are Bob, the cfo agent on Lightbulb.")
    assert "Reconciles Stripe payouts. Chases overdue invoices." in card
    assert "Spend, budgets" not in card  # role fallback only without a "What you do" section
    assert card.endswith("Tools: none.")


@pytest.mark.asyncio
async def test_renamed_starter_agent_gets_role_description_without_what_you_do(monkeypatch):
    soul = "<!-- lightbulb:generated -->\n\n# Zed\n\nYou are Zed, the cfo agent on Lightbulb.\n"
    _profile(monkeypatch, {"SOUL.md": soul, ".revision": "r1"}, {}, {"name": "Zed", "role": "cfo"})
    card = await jev.agent_card("org_1", "a1", "Zed")
    assert (
        card
        == "Zed (role: cfo). You are Zed, the cfo agent on Lightbulb. Spend, budgets, invoices, revenue. Tools: none."
    )


@pytest.mark.asyncio
async def test_card_never_contains_secret_values(monkeypatch):
    config = {
        "mcp_servers": {
            "stripe": {"env": {"STRIPE_KEY": "sk_live_LEAKME"}, "headers": {"Authorization": "Bearer ghp_LEAKME"}},
            "github": {"url": "https://x.test/?token=LEAKME"},
        }
    }
    facts = json.dumps({"company_stripe": "connected", "email_address": "a@LEAKME.test", "owner_id": "org_LEAKME"})
    reads, _ = _profile(
        monkeypatch,
        {"SOUL.md": SOUL_WHAT, ".revision": "r1", ".env": "STRIPE_KEY=sk_live_LEAKME", "lightbulb-facts.json": facts},
        config,
        {"name": "Bob", "role": "cfo"},
    )
    card = await jev.agent_card("org_1", "a1", "Bob")
    assert "LEAKME" not in card
    assert "Tools: github, stripe." in card
    assert not any(key.endswith(".env") for key in reads)


@pytest.mark.asyncio
async def test_card_cache_invalidates_on_revision_change(monkeypatch):
    files = {"SOUL.md": SOUL_WHAT, ".revision": "r1"}
    _, paperclip = _profile(monkeypatch, files, {}, {"name": "Bob", "role": "cfo"})
    first = await jev.agent_card("org_1", "a1", "Bob")
    assert await jev.agent_card("org_1", "a1", "Bob") == first
    assert paperclip.await_count == 1
    files["SOUL.md"] = "# Bob\n\nYou are Bob.\n\n## What you do\n\nRuns payroll.\n"
    files[".revision"] = "r2"
    changed = await jev.agent_card("org_1", "a1", "Bob")
    assert "Runs payroll." in changed and changed != first
    assert paperclip.await_count == 2


@pytest.mark.asyncio
async def test_card_is_capped_at_600_chars_truncating_soul_first(monkeypatch):
    long = "## What you do\n\n" + " ".join(f"Does task number {i}." for i in range(200))
    config = {"mcp_servers": {f"tool{i}": {} for i in range(5)}}
    _profile(monkeypatch, {"SOUL.md": long, ".revision": "r1"}, config, {"name": "Bob", "role": "cfo"})
    card = await jev.agent_card("org_1", "a1", "Bob", "Last spoke 3m ago.")
    assert len(card) <= 600
    assert card.endswith("Tools: tool0, tool1, tool2, tool3, tool4. Last spoke 3m ago.")


@pytest.mark.asyncio
async def test_card_sources_failing_degrade_to_display_name(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(jev, "_read", boom)
    monkeypatch.setattr(jev, "_registry_agent", AsyncMock(side_effect=RuntimeError("paperclip down")))
    assert await jev.agent_card("org_1", "a1", "Finance") == "Finance. Tools: none."


def test_a_loser_outwaits_the_winners_whole_budget_plus_its_write():
    # Equal timeouts let a slow winner's decision write land after the loser gave up and fell
    # back to dispatch-all: two responders for one message.
    assert jev._LOSER_WAIT_S >= jev._DECIDE_BUDGET_S + 3


@pytest.mark.asyncio
async def test_concurrent_first_key_reads_all_get_the_key_from_one_read(monkeypatch):
    # Dev 2026-09-23: two consumers on one task asked at once; the second saw the timestamp the
    # first had already stamped, no key yet, and reported Jev "disabled" 170 ms later.
    import asyncio as _asyncio

    reads = []

    def slow_read(name):
        reads.append(name)
        import time as _t

        _t.sleep(0.05)
        return KEY

    monkeypatch.setattr(jev, "_read_secret", slow_read)
    keys = await _asyncio.gather(*(jev._gateway_key() for _ in range(5)))
    assert keys == [KEY] * 5
    assert len(reads) == 1


def test_captionless_media_is_visible_to_jev():
    line = jev._line(
        {
            "author": {"id": "u", "kind": "human"},
            "text": "",
            "attachments": [
                {"contentType": "image/png"},
                {"contentType": "video/mp4"},
                {"contentType": "application/pdf", "fileName": "deck.pdf"},
            ],
        }
    )
    assert line["text"] == "[image] [video] [file: deck.pdf]"
    assert (
        jev._line({"author": {"id": "u"}, "text": "hi", "attachments": [{"contentType": "image/png"}]})["text"] == "hi"
    )
