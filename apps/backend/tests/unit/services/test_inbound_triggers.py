"""Tests for the Automations Phase 2 trigger-seed service.

Patches ``PaperclipRepo`` (company
resolution), ``get_admin_session_cookie``, and ``PaperclipAdminClient`` so
nothing here touches a real Paperclip deployment.
"""

import contextlib
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _fake_admin(
    *,
    friday_id="friday_1",
    routine_id="rt_1",
    public_id="pub_1",
    secret_material=None,
    trigger_id=None,
    description="Chase every failed invoice personally. Never mention the word dunning.",
):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[{"id": friday_id, "role": "ceo"}])
    admin.create_routine = AsyncMock(return_value={"id": routine_id})
    trigger = {"publicId": public_id}
    # The internal trigger id — the auto_vercel arm (rotate-secret) needs it.
    if trigger_id is not None:
        trigger["id"] = trigger_id
    trigger_resp = {"trigger": trigger}
    if secret_material is not None:
        trigger_resp["secretMaterial"] = secret_material
    admin.create_routine_trigger = AsyncMock(return_value=trigger_resp)
    admin.rotate_routine_trigger_secret = AsyncMock(return_value={})
    admin.patch_routine = AsyncMock(return_value={})
    admin.delete_routine = AsyncMock(return_value={})
    # The ATTACH path's two extra calls: the company's routine list is the
    # tenancy fence, and only the trigger it added may be unwound. The list
    # carries `routine_id` so `_fake_admin(routine_id=...)` names the attach
    # target with the same knob it names a created routine.
    admin.list_routines = AsyncMock(return_value=[{"id": routine_id, "title": "Revenue recovery"}])
    admin.delete_routine_trigger = AsyncMock(return_value={})
    # The APPEND path reads the routine's current description before patching
    # it — a hand-written body the platform block must land AFTER, not over.
    admin.get_routine = AsyncMock(return_value={"id": routine_id, "description": description})
    return admin


# --- auto_github ------------------------------------------------------------
#
# GitHub HTTP is mocked by patching the module's httpx.AsyncClient directly
# (same approach as test_clerk_admin.py — respx 0.20.2 + newer httpx have a
# route-matching incompat). The seed opens TWO clients: the Paperclip one
# (whose requests never fire — the admin client and session are mocked) and
# the GitHub one, so a single fake client serves both.


def _gh_token_patch(token="gh_tok_1"):
    credential = {"token": token} if token else None
    return patch(
        "core.services.inbound_triggers.integration_secret_store.get",
        AsyncMock(return_value=credential),
    )


def _gh_response(status_code: int, json_body=None) -> httpx.Response:
    # A real Response (with a request attached) so raise_for_status() works.
    return httpx.Response(
        status_code,
        json=json_body if json_body is not None else {},
        request=httpx.Request("POST", "https://api.github.com/x"),
    )


def _gh_http_patch(response: httpx.Response):
    """Patch httpx.AsyncClient in the module with a fake whose get/post/delete
    answer ``response``. Returns (patcher, client) so tests can assert on the
    call."""
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    client.delete = AsyncMock(return_value=response)

    @asynccontextmanager
    async def _fake(*args, **kwargs):
        yield client

    return patch("core.services.inbound_triggers.httpx.AsyncClient", new=_fake), client


@pytest.mark.asyncio
async def test_list_github_repos_maps_full_names():
    from core.services.inbound_triggers import list_github_repos

    p_http, gh_client = _gh_http_patch(_gh_response(200, [{"full_name": "acme/site"}, {"full_name": "acme/api"}]))
    with _gh_token_patch(), p_http:
        out = await list_github_repos("owner_1")
    assert out == [
        {"id": "acme/site", "label": "acme/site"},
        {"id": "acme/api", "label": "acme/api"},
    ]
    assert gh_client.get.await_args.args[0] == "https://api.github.com/user/repos"
    assert gh_client.get.await_args.kwargs["params"] == {"per_page": 100, "sort": "updated"}


@pytest.mark.asyncio
async def test_list_github_repos_not_connected_raises_lookup_error():
    from core.services.inbound_triggers import list_github_repos

    with _gh_token_patch(token=None), pytest.raises(LookupError, match="not connected"):
        await list_github_repos("owner_1")


# --- auto_vercel ------------------------------------------------------------
#
# Same mocking approach as auto_github above. `_gh_token_patch` is reused for
# the Vercel credential — it patches the shared integration_secret_store.get,
# and the paste rail stores the same {"token": ...} shape for vercel_cli.


@pytest.mark.asyncio
async def test_list_vercel_projects_maps_ids_and_names():
    from core.services.inbound_triggers import list_vercel_projects

    p_http, vc_client = _gh_http_patch(
        _gh_response(200, {"projects": [{"id": "prj_1", "name": "site"}, {"id": "prj_2", "name": "api"}]})
    )
    with _gh_token_patch(token="vc_tok_1"), p_http:
        out = await list_vercel_projects("owner_1")
    assert out == [
        {"id": "prj_1", "label": "site"},
        {"id": "prj_2", "label": "api"},
    ]
    assert vc_client.get.await_args.args[0] == "https://api.vercel.com/v9/projects"


@pytest.mark.asyncio
async def test_list_vercel_projects_not_connected_raises_lookup_error():
    from core.services.inbound_triggers import list_vercel_projects

    with _gh_token_patch(token=None), pytest.raises(LookupError, match="not connected"):
        await list_vercel_projects("owner_1")


# --- auto_google_watch --------------------------------------------------------
#
# The gog layer is mocked at google_watch's runner helper (_run_gog), with _gog_env stubbed so no
# credential paths are touched — the REAL command construction runs, and the tests assert exact
# argv. The fence being pinned: caller data (the account email, the channel token) rides only as
# discrete argv elements.
#
# The agent-side state file is a profile write now, not an EFS one: it lands in EVERY roster
# profile under `home/google-watch/`, which is `~/google-watch/` to the agent's shell. So
# `_watch_state_patch` captures what write_profile_subtree was handed per agent instead of reading
# a file back off disk — and that per-agent fan-out is itself the thing to assert, since the
# trigger names a routine, not an agent.


def _google_accounts_patch(emails=("ada@example.com",)):
    return patch(
        "core.services.inbound_triggers.google_accounts_repo.list_accounts",
        AsyncMock(return_value=[{"email": e} for e in emails]),
    )


class _WatchStateWrites:
    """Captures ``hermes_profiles.write_profile_subtree`` calls: {agent_id: {rel_path: bytes}}."""

    def __init__(self):
        self.by_agent: dict[str, dict[str, bytes]] = {}

    def __call__(self, owner_id, agent_id, files, prune):
        self.by_agent.setdefault(agent_id, {}).update(files)

    def state(self, agent_id="agent_ceo", public_id="pub_1"):
        return json.loads(self.by_agent[agent_id][f"home/google-watch/{public_id}.json"])


@contextlib.contextmanager
def _watch_state_patch(roster=("agent_ceo",)):
    """Yields the recorder, with the roster read and the profile writer both stubbed."""
    writes = _WatchStateWrites()
    with (
        patch("core.services.hermes_profiles.write_profile_subtree", writes),
        patch("core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": a} for a in roster])),
    ):
        yield writes


@pytest.mark.asyncio
async def test_list_google_accounts_maps_emails():
    from core.services.inbound_triggers import list_google_accounts

    with _google_accounts_patch(emails=("ada@example.com", "team@example.com")):
        out = await list_google_accounts("owner_1")
    assert out == [
        {"id": "ada@example.com", "label": "ada@example.com"},
        {"id": "team@example.com", "label": "team@example.com"},
    ]


@pytest.mark.asyncio
async def test_list_google_accounts_not_connected_raises_lookup_error():
    from core.services.inbound_triggers import list_google_accounts

    with _google_accounts_patch(emails=()), pytest.raises(LookupError, match="not connected"):
        await list_google_accounts("owner_1")


def test_google_watch_stop_argv():
    """stop_watch's fixed argv for both surfaces — drive uses the dedicated
    stop command, calendar rides the generic channels.stop Discovery call."""
    from core.services import google_watch

    run_gog = MagicMock(return_value="{}")
    with (
        patch("core.services.google_watch._gog_env", return_value={"FAKE": "env"}),
        patch("core.services.google_watch._run_gog", run_gog),
    ):
        google_watch._stop_watch_sync("owner_1", "ada@example.com", "drive.changed", "chan_1", "res_1")
        assert run_gog.call_args.args[0] == [
            "gog",
            "drive",
            "changes",
            "stop",
            "chan_1",
            "res_1",
            "-a",
            "ada@example.com",
        ]

        google_watch._stop_watch_sync("owner_1", "ada@example.com", "calendar.changed", "chan_2", "res_2")
        argv = run_gog.call_args.args[0]
        assert argv[:6] == ["gog", "api", "call", "calendar", "v3", "channels.stop"]
        assert argv[6] == "--body"
        assert json.loads(argv[7]) == {"id": "chan_2", "resourceId": "res_2"}
        assert argv[8:] == ["--allow-write", "--force", "-a", "ada@example.com"]


# --- gmail (per-event registration: poll / owner_setup) -----------------------
#
# Both shapes ride the google block; the event's `registration` overrides the
# block-level auto_google_watch. Same mocking approach as the watch tests.

_GMAIL_TOPIC = "projects/my-project/topics/isol8-gmail"


def test_google_watch_gmail_argv():
    """The fixed argv for the gmail watch pair — caller data rides only as
    discrete argv elements — and the expiration parse. (Renewal has no
    backend argv: the routine's own agent runs `gog gmail watch renew`
    in-container.)"""
    from core.services import google_watch

    run_gog = MagicMock(return_value=json.dumps({"historyId": "7", "expiration": "1799999999000"}))
    with (
        patch("core.services.google_watch._gog_env", return_value={"FAKE": "env"}),
        patch("core.services.google_watch._run_gog", run_gog),
    ):
        out = google_watch._start_gmail_watch_sync("owner_1", "ada@example.com", _GMAIL_TOPIC)
        assert out == {"expiration_ms": 1799999999000}
        assert run_gog.call_args.args[0] == [
            "gog",
            "gmail",
            "watch",
            "start",
            "--topic",
            _GMAIL_TOPIC,
            "--label-ids",
            "INBOX",
            "-a",
            "ada@example.com",
        ]

        google_watch._stop_gmail_watch_sync("owner_1", "ada@example.com")
        assert run_gog.call_args.args[0] == ["gog", "gmail", "watch", "stop", "-a", "ada@example.com"]


def test_parse_gmail_watch_requires_expiration():
    """A watch response without `expiration` is as fatal as the call failing —
    the seeded state file could never say when to re-arm."""
    from core.services.google_watch import _parse_gmail_watch

    with pytest.raises(RuntimeError, match="no expiration"):
        _parse_gmail_watch(json.dumps({"historyId": "7"}))


# --- auto_meta ---------------------------------------------------------------
#
# The first APP-LEVEL shape: the seed registers nothing owner-specific with
# Meta beyond subscribing the asset, and records the asset↔trigger binding the
# ownerless ingress route routes by. Same fake-collaborator idiom as above; the
# Graph calls are the module's httpx.AsyncClient, with GET answering
# /me/accounts and POST/DELETE answering subscribed_apps.

_PAGES = {"data": [{"id": "1112223334", "name": "Acme Page", "access_token": "page_tok_1"}]}
_IG_CREDS = {"access_token": "ig_tok_1", "user_id": "7778889990", "username": "acme.co"}


def _meta_http_patch(*, pages=_PAGES, subscribe=None):
    """Fake httpx.AsyncClient whose GET answers /me/accounts and whose
    POST/DELETE answer subscribed_apps."""
    client = MagicMock()
    client.get = AsyncMock(return_value=_gh_response(200, pages))
    client.post = AsyncMock(return_value=subscribe if subscribe is not None else _gh_response(200, {"success": True}))
    client.delete = AsyncMock(return_value=_gh_response(200, {"success": True}))

    @asynccontextmanager
    async def _fake(*args, **kwargs):
        yield client

    return patch("core.services.inbound_triggers.httpx.AsyncClient", new=_fake), client


def _ig_creds_patch(creds=None):
    return patch(
        "core.services.inbound_triggers._read_instagram_credentials",
        return_value=dict(creds if creds is not None else _IG_CREDS),
    )


# --- Meta resource listers ---------------------------------------------------


@pytest.mark.asyncio
async def test_list_meta_pages_maps_and_strips_tokens():
    from core.services.inbound_triggers import list_meta_pages

    p_http, _ = _meta_http_patch()
    with (
        p_http,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
    ):
        out = await list_meta_pages("owner_1")
    # The picker feed is owner-facing JSON — a Page access token must never
    # ride out of the resources endpoint.
    assert out == [{"id": "1112223334", "label": "Acme Page"}]


@pytest.mark.asyncio
async def test_list_meta_pages_never_substitutes_user_token_for_page_token():
    from core.services.inbound_triggers import list_meta_pages

    p_http, _ = _meta_http_patch(pages={"data": [{"id": "1112223334", "name": "Acme Page"}]})
    with (
        p_http,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
        pytest.raises(RuntimeError, match="without Page access tokens"),
    ):
        await list_meta_pages("owner_1")


@pytest.mark.asyncio
async def test_list_meta_pages_not_connected():
    from core.services.inbound_triggers import list_meta_pages

    with (
        patch("core.services.inbound_triggers.integration_secret_store.get", AsyncMock(return_value=None)),
        pytest.raises(LookupError, match="Meta Ads is not connected"),
    ):
        await list_meta_pages("owner_1")


@pytest.mark.asyncio
async def test_list_meta_pages_unrecognised_body_is_not_guessed_at():
    """A 200 without a `data` list is the provider misbehaving — the router
    turns this into a 502 rather than the lister inventing an empty picker."""
    from core.services.inbound_triggers import list_meta_pages

    p_http, _ = _meta_http_patch(pages={"paging": {}})
    with (
        p_http,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
        pytest.raises(RuntimeError, match="no page list"),
    ):
        await list_meta_pages("owner_1")


@pytest.mark.asyncio
async def test_list_instagram_accounts_maps_and_strips_token():
    from core.services.inbound_triggers import list_instagram_accounts

    with _ig_creds_patch():
        assert await list_instagram_accounts("owner_1") == [{"id": "7778889990", "label": "acme.co"}]


@pytest.mark.asyncio
async def test_list_instagram_accounts_not_connected():
    from core.services.inbound_triggers import list_instagram_accounts

    with (
        patch("core.services.inbound_triggers._read_instagram_credentials", side_effect=FileNotFoundError()),
        pytest.raises(LookupError, match="Instagram is not connected"),
    ):
        await list_instagram_accounts("owner_1")


# ---------------------------------------------------------------------------
# Assignee threading — a seeded automation is not necessarily Friday's
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Registry title inversion — how a published routine names its automation
# ---------------------------------------------------------------------------


def test_automation_titles_are_unique_across_the_registry():
    """The marketplace recovers (provider, event) from a seeded routine's
    TITLE, so two events rendering the same title would make that ambiguous
    and silently ship the wrong requirement. Keep titles unique."""
    from core.services.credentials import registry

    titles = [
        p.inbound.template.title.format(event_label=event.label)
        for p in registry.all_providers().values()
        if p.inbound is not None
        for event in p.inbound.events
    ]
    assert len(set(titles)) == len(titles)


def test_automation_requirement_flags_the_buyer_specific_resource():
    """guided_paste needs nothing but the proxy URL; every auto/poll shape
    binds a provider-side resource only the BUYER can name."""
    from core.services.inbound_triggers import automation_requirement

    assert automation_requirement("stripe", "charge.refunded") == {
        "provider": "stripe",
        "event_id": "charge.refunded",
        "needs_resource": False,
    }
    assert automation_requirement("github", "issues")["needs_resource"] is True
    assert automation_requirement("google", "calendar.changed")["needs_resource"] is True
    assert automation_requirement("notion", "whatever") is None
    assert automation_requirement("github", "not.an.event") is None
    assert automation_requirement("no_such_provider", "issues") is None


# --- attach to an existing routine (routine_id) ------------------------------
#
# The second half of the guided flow: arm this event on an automation the
# owner already wrote. The whole point is that NOTHING about that routine
# changes — the tests below pin the create calls that must NOT happen as
# firmly as the trigger call that must.


# --- append, never overwrite (the shapes that write the routine's body) -----
#
# auto_google_watch / owner_setup / poll keep their automation alive by
# writing instructions into its description. On the CREATE path they compose
# the whole thing; on an ATTACH the body is the owner's, so the same text
# lands as a delimited block at the END. Refusing instead (what this
# replaced) was a dead end: the machinery already supports the attach.


# --- owner-written title / description overrides ----------------------------


# ── multi-event: one trigger, many events ───────────────────────────────────
#
# A Paperclip webhook trigger has no event filter, and Stripe/GitHub/Vercel/
# Meta all let one endpoint subscribe to many events — so the founder's
# three-event Revenue recovery routine is ONE trigger and ONE dashboard paste,
# not three. The events that CAN'T share a trigger (a Google watch, a Gmail
# poll) are the 400 below.
