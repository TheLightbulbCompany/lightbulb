"""The door must match the record: replaced builds stop, a stale winner is put right, a tighten that
can't build says so, and a version is stamped with the door it was built with.

In its own file so the fixtures are shared with test_apps_service without touching it.
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tests.unit.services.test_apps_service import (  # noqa: F401 - fixtures
    OWNER,
    PERSON,
    SRC_V2,
    _closed_after_public,
    _go_live,
    _instant_rollbacks,
    _land,
    _ready_preview,
    _source,
    published,
    world,
)

from core.services import apps_service as svc
from core.services import apps_sweeper, vercel_client


@pytest.fixture
def serving(world, monkeypatch):  # noqa: F811
    """What the custom domain points at, as Vercel's alias lookup reports it. ``cancel``: the cancel's status."""
    state = {"deployment": None, "cancel": 200, "alias": 200}
    real = world.vercel.handler

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/v4/aliases/"):
            world.vercel.calls.append(req)
            return httpx.Response(state["alias"], json={"deploymentId": state["deployment"]})
        if req.url.path.endswith("/cancel"):
            world.vercel.calls.append(req)
            return httpx.Response(state["cancel"], json={})
        return real(req)

    monkeypatch.setattr(vercel_client, "_transport", httpx.MockTransport(handler))
    return state


def _cancelled(world):  # noqa: F811
    return [p.split("/")[3] for m, p in world.vercel.paths("PATCH") if p.endswith("/cancel")]


def _alias_reads(world):  # noqa: F811
    return [p for _, p in world.vercel.paths("GET") if p.startswith("/v4/aliases/")]


async def _record(app_id):
    return svc.plain(await svc.apps_repo.get(app_id, consistent_read=True))


async def _pending(app_id):
    return (await _record(app_id))["publish_pending"]["deployment_id"]


async def _public_live(world):  # noqa: F811
    src, _, _ = await _ready_preview(world)
    await svc.update_settings(OWNER, src["app_id"], open_to="public")
    await svc.status(OWNER, src["app_id"])
    await _go_live(world, src)
    return src


async def _verified_domain(app_id):
    app = await _record(app_id)
    await svc.apps_repo.patch(app_id, {"domain": {**app["domain"], "verified": True}})


async def _settle_preview(world, app_id):  # noqa: F811
    """A sharing change rebuilds the preview too: with it landed, only the marker can bring the sweeper."""
    app = await _record(app_id)
    if app.get("preview_pending"):
        world.vercel.finish(app["preview_pending"]["deployment_id"])
        await svc.status(OWNER, app_id)


async def _stale_won(world, serving):  # noqa: F811
    """Public live; a publish builds with the door off; the owner closes it; the replacement lands; then the
    replaced build wins the domain anyway. Returns (app_id, replaced, replacement)."""
    src = await _public_live(world)
    s2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=s2["source_id"], actor=PERSON)
    replaced = await _pending(src["app_id"])
    await svc.update_settings(OWNER, src["app_id"], open_to="room")
    replacement = await _pending(src["app_id"])
    assert replaced != replacement
    await _verified_domain(src["app_id"])
    serving["deployment"] = replacement
    await _land(world, src["app_id"])
    await _settle_preview(world, src["app_id"])
    assert not (await _record(src["app_id"])).get("preview_pending")
    serving["deployment"] = replaced
    return src["app_id"], replaced, replacement


# --- 1. a replaced build is stopped ---------------------------------------------------------------


async def test_tighten_cancels_the_publish_it_replaces(world, serving):  # noqa: F811
    src = await _public_live(world)
    s2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=s2["source_id"], actor=PERSON)
    replaced = await _pending(src["app_id"])
    await svc.update_settings(OWNER, src["app_id"], open_to="room")
    assert _cancelled(world) == [replaced]


async def test_flip_open_and_straight_back_cancels_the_open_build(world, serving):  # noqa: F811
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await svc.update_settings(OWNER, src["app_id"], open_to="public")
    opened = await _pending(src["app_id"])
    await svc.update_settings(OWNER, src["app_id"], open_to="room")
    assert _cancelled(world) == [opened]


async def test_a_new_publish_cancels_the_publish_it_replaces(world, serving):  # noqa: F811
    src, _, _ = await _ready_preview(world)
    await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    first = await _pending(src["app_id"])
    s2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=s2["source_id"], actor=PERSON)
    assert await _pending(src["app_id"]) != first and _cancelled(world) == [first]


async def test_put_back_cancels_the_publish_in_flight_both_ways(world, serving):  # noqa: F811
    src = await _closed_after_public(world)
    s3 = await _source({**SRC_V2, "new.txt": "x"}, "c9")
    await svc.publish(OWNER, src["app_id"], source_id=s3["source_id"], actor=PERSON)
    flying = await _pending(src["app_id"])
    res = await svc.rollback(OWNER, src["app_id"], version=1)  # a rebuild: v1 had another door
    assert res["rebuilding"] and _cancelled(world) == [flying]
    assert "won't go live" in res["error"]["message"] and "cancelled" not in res["error"]["message"]
    rebuild = await _pending(src["app_id"])
    res = await svc.rollback(OWNER, src["app_id"], version=3)  # instant: same door
    assert "rebuilding" not in res and _cancelled(world) == [flying, rebuild]
    assert not (await _record(src["app_id"])).get("publish_pending")


async def test_a_finished_or_refused_cancel_never_stops_the_change(world, serving, caplog):  # noqa: F811
    src = await _public_live(world)
    s2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=s2["source_id"], actor=PERSON)
    serving["cancel"] = 400  # already READY, ERROR or CANCELED
    with caplog.at_level(logging.WARNING):
        await svc.update_settings(OWNER, src["app_id"], open_to="room")
    assert not [r for r in caplog.records if "cancel" in r.getMessage()]
    serving["cancel"] = 500
    with caplog.at_level(logging.WARNING):
        await svc.update_settings(OWNER, src["app_id"], open_to="public")
    assert [r for r in caplog.records if "cancel" in r.getMessage()]
    assert (await _record(src["app_id"]))["publish_pending"]


# --- 2. a stale build that won anyway -------------------------------------------------------------


async def test_sweeper_puts_the_recorded_build_back_when_a_replaced_one_won(world, serving):  # noqa: F811
    app_id, _, replacement = await _stale_won(world, serving)
    assert await apps_sweeper.sweep_once() == 1  # nothing pending: only the marker brings it here
    assert _instant_rollbacks(world) == [f"/v1/projects/prj_1/rollback/{replacement}"]
    app = await _record(app_id)
    assert app["rolled_back"] is True and app["versions"][-1]["deployment_id"] == replacement


async def test_no_repoint_while_the_recorded_build_serves(world, serving):  # noqa: F811
    _, _, replacement = await _stale_won(world, serving)
    serving["deployment"] = replacement
    await apps_sweeper.sweep_once()
    assert _alias_reads(world) and _instant_rollbacks(world) == []


async def test_no_repoint_for_a_deployment_that_was_never_replaced(world, serving):  # noqa: F811
    """Vercel moving the alias a beat after READY, or a person in Vercel's dashboard: not ours to undo."""
    await _stale_won(world, serving)
    serving["deployment"] = "dpl_someone_elses"
    await apps_sweeper.sweep_once()
    assert _instant_rollbacks(world) == []


async def test_no_repoint_while_a_new_build_is_pending(world, serving):  # noqa: F811
    app_id, _, _ = await _stale_won(world, serving)
    s3 = await _source({**SRC_V2, "new.txt": "x"}, "c9")
    serving["deployment"] = None  # publish's own read is the check above; this one has a build pending
    await svc.publish(OWNER, app_id, source_id=s3["source_id"], actor=PERSON)
    serving["deployment"] = (await _record(app_id))["replaced_builds"]["deployments"][0]
    n = len(_alias_reads(world))
    await apps_sweeper.sweep_once()
    assert len(_alias_reads(world)) == n and _instant_rollbacks(world) == []  # it takes the domain when it lands


async def test_the_marker_expires_and_the_app_leaves_the_sweep(world, serving):  # noqa: F811
    app_id, replaced, _ = await _stale_won(world, serving)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await svc.apps_repo.patch(app_id, {"replaced_builds": {"deployments": [replaced], "until": past}})
    n = len(_alias_reads(world))
    assert await apps_sweeper.sweep_once() == 0
    await svc.status(OWNER, app_id)
    assert len(_alias_reads(world)) == n and _instant_rollbacks(world) == []


async def test_an_app_with_no_replaced_build_is_never_looked_up(world, serving):  # noqa: F811
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await _verified_domain(src["app_id"])
    assert await apps_sweeper.sweep_once() == 0
    await svc.status(OWNER, src["app_id"])
    assert _alias_reads(world) == []


async def test_an_unreadable_alias_changes_nothing_and_reports_no_error(world, serving):  # noqa: F811
    app_id, _, _ = await _stale_won(world, serving)
    for status in (404, 403):
        serving["alias"] = status
        app = await svc._owned(OWNER, app_id)
        rows, transient = await svc._progress(app)
        assert transient is None and _instant_rollbacks(world) == []


# --- 3. the serving source survives pruning, and a tighten that can't build says so ----------------


async def _put_back_then_uploads(world):  # noqa: F811
    src = await _public_live(world)
    await _go_live(world, await _source(SRC_V2, "c2"))
    await svc.rollback(OWNER, src["app_id"], version=1)  # same door, instant: live_source_id is cleared
    for i in range(11):
        latest = await _source({**SRC_V2, "n.txt": str(i)}, f"up{i}")
        await svc.apps_repo.patch(src["app_id"], {"preview_want": latest["source_id"]})  # what `up` does
    return src


async def test_pruning_keeps_the_serving_source_so_a_tighten_builds_it(world, serving):  # noqa: F811
    src = await _put_back_then_uploads(world)
    assert src["source_id"] in (await _record(src["app_id"]))["sources"]
    await svc.update_settings(OWNER, src["app_id"], open_to="room")
    app = await _record(src["app_id"])
    assert app["publish_pending"]["source_id"] == src["source_id"] and app["publish_pending"]["open_to"] == "room"


async def test_a_tighten_with_no_source_is_reported_and_retried_not_saved(world, serving):  # noqa: F811
    src = await _put_back_then_uploads(world)
    app = await _record(src["app_id"])
    sources = {k: v for k, v in app["sources"].items() if k != src["source_id"]}
    await svc.apps_repo.patch(src["app_id"], {"sources": sources})
    before = app["env_fp"]["production"]

    await svc.update_settings(OWNER, src["app_id"], open_to="room")
    await _settle_preview(world, src["app_id"])
    app = await _record(src["app_id"])
    assert app["env_fp"]["production"] == before and not app.get("publish_pending")
    assert app["publish_error"]["code"] == "sharing_not_applied"
    result = await svc._result(app, await svc._rows(app), None)
    assert result["error"]["code"] == "sharing_not_applied"  # what a builder and Canvas read
    pushes = len(world.vercel.env_posts())
    await svc.status(OWNER, src["app_id"])
    assert len(world.vercel.env_posts()) == pushes + 1  # not saved, so the next read tries again

    # The way out: a new publish goes through, and then the fingerprint settles.
    s3 = await _source({**SRC_V2, "new.txt": "x"}, "c9")
    await svc.publish(OWNER, src["app_id"], source_id=s3["source_id"], actor=PERSON)
    await svc.status(OWNER, src["app_id"])
    app = await _record(src["app_id"])
    assert app["publish_pending"]["source_id"] == s3["source_id"] and app["publish_pending"]["open_to"] == "room"
    assert app["env_fp"]["production"] != before and not app.get("publish_error")


# --- 4. the stamp is the door the build got --------------------------------------------------------


async def test_a_tighten_during_the_upload_is_rebuilt_with_the_right_door(world, serving, monkeypatch):  # noqa: F811
    src = await _public_live(world)
    s2 = await _source(SRC_V2, "c2")
    real = svc.vercel.create_deployment
    fired = {}

    async def create(*a, **k):
        d = await real(*a, **k)  # built with the door off
        if not fired:
            fired["dep"] = d["id"]
            await svc.update_settings(OWNER, src["app_id"], open_to="room")  # its own build goes pending
            fired["theirs"] = await _pending(src["app_id"])
        return d

    monkeypatch.setattr(svc.vercel, "create_deployment", create)
    await svc.publish(OWNER, src["app_id"], source_id=s2["source_id"], actor=PERSON)
    app = await _record(src["app_id"])
    assert app["publish_pending"]["deployment_id"] == fired["dep"] and app["publish_pending"]["open_to"] == "public"
    assert fired["theirs"] in _cancelled(world)  # the overwritten build is stopped, not left to land

    await svc.status(OWNER, src["app_id"])  # the very next step replaces it, before it can go READY
    assert await _pending(src["app_id"]) != fired["dep"] and fired["dep"] in _cancelled(world)
    app = await _record(src["app_id"])
    assert app["publish_pending"]["source_id"] == s2["source_id"]  # the new code still goes out
    st = await _land(world, src["app_id"])
    app = await _record(src["app_id"])
    assert app["versions"][-1]["open_to"] == "room" and app["versions"][-1]["source_id"] == s2["source_id"]
    assert app["versions"][-1]["deployment_id"] != fired["dep"] and st["live_version"] == 2


async def test_a_ready_build_with_the_wrong_door_is_never_recorded(world, serving):  # noqa: F811
    src = await _public_live(world)
    s2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=s2["source_id"], actor=PERSON)
    dep = await _pending(src["app_id"])
    await svc.apps_repo.patch(src["app_id"], {"open_to": "room"})  # the record moved; the env push comes this read
    world.vercel.finish(dep)
    st = await svc.status(OWNER, src["app_id"])
    assert st["live_version"] == 1 and await _pending(src["app_id"]) != dep


async def test_a_stamp_reads_the_env_not_a_record_whose_push_is_still_going(world, serving, monkeypatch):  # noqa: F811
    """The record says room but the push hasn't reached Vercel: what publish builds now has the door off."""
    src = await _public_live(world)
    s2 = await _source(SRC_V2, "c2")
    lease = await svc.apps_repo.take_lease(src["app_id"], seconds=60)  # another read is mid-push
    await svc.apps_repo.patch(src["app_id"], {"open_to": "room"})
    await svc.publish(OWNER, src["app_id"], source_id=s2["source_id"], actor=PERSON)
    assert (await _record(src["app_id"]))["publish_pending"]["open_to"] == "public"
    await svc.apps_repo.release_lease(src["app_id"], lease)


async def test_a_pending_from_before_the_stamp_is_left_alone(world, serving):  # noqa: F811
    src = await _public_live(world)
    s2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=s2["source_id"], actor=PERSON)
    app = await _record(src["app_id"])
    dep = app["publish_pending"]["deployment_id"]
    await svc.apps_repo.patch(
        src["app_id"], {"publish_pending": {k: v for k, v in app["publish_pending"].items() if k != "open_to"}}
    )
    world.vercel.finish(dep)
    st = await svc.status(OWNER, src["app_id"])
    assert st["live_version"] == 2 and (await _record(src["app_id"]))["versions"][-1]["open_to"] == "public"
