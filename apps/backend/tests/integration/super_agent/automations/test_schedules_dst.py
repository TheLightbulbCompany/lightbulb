"""Temporal Schedules across DST on a real dev server (spec 06 §8 S-01…S-08).

Each test builds the Schedule exactly as `automation_schedule.build` does, pins `start_at`, and reads
`describe().info.next_action_times` (UTC)."""

from datetime import datetime, timezone


from core.services import automation_schedule as asch


def _row(ws, sid, cron, tz, **over):
    return {"pk": f"WS#{ws}", "scheduleId": sid, "cron": cron, "tz": tz, "status": "active"} | over


async def _next(client, row, start: datetime, n=4):
    s = asch.build(row)
    s.spec.start_at = start
    h = await client.create_schedule(asch.schedule_id(row), s)
    try:
        return [t.isoformat() for t in (await h.describe()).info.next_action_times[:n]]
    finally:
        await h.delete()


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


async def test_S01_ny_spring_forward(client, ws):
    got = await _next(client, _row(ws, "s1", ["0 9 * * *"], "America/New_York"), _utc(2027, 3, 12))
    assert got == [
        "2027-03-12T14:00:00+00:00",
        "2027-03-13T14:00:00+00:00",
        "2027-03-14T13:00:00+00:00",
        "2027-03-15T13:00:00+00:00",
    ]


async def test_S02_ny_fall_back(client, ws):
    got = await _next(client, _row(ws, "s2", ["0 9 * * *"], "America/New_York"), _utc(2027, 11, 6))
    assert got[:3] == ["2027-11-06T13:00:00+00:00", "2027-11-07T14:00:00+00:00", "2027-11-08T14:00:00+00:00"]


async def test_S03_spring_gap_0230(client, ws):
    # Recorded: 02:30 does not exist in New York on 2027-03-14 and Temporal skips that day (no 03:30
    # substitute). The editor shows a hint for 01:00–03:00 local times (spec 06 §3.4).
    got = await _next(client, _row(ws, "s3", ["30 2 * * *"], "America/New_York"), _utc(2027, 3, 13), n=3)
    assert got == ["2027-03-13T07:30:00+00:00", "2027-03-15T06:30:00+00:00", "2027-03-16T06:30:00+00:00"]


async def test_S04_fall_back_0130_fires_at_both(client, ws):
    # Recorded: 01:30 happens twice in New York on 2027-11-07 and Temporal fires at BOTH (05:30Z EDT and
    # 06:30Z EST). Spec 06 S-04 wanted once; TriggerFireWorkflow (Task 5) must key the schedule trigger on
    # the local wall-clock minute so the second fire maps to the same trigger id and is not admitted twice.
    got = await _next(client, _row(ws, "s4", ["30 1 * * *"], "America/New_York"), _utc(2027, 11, 6), n=4)
    assert got == [
        "2027-11-06T05:30:00+00:00",
        "2027-11-07T05:30:00+00:00",
        "2027-11-07T06:30:00+00:00",
        "2027-11-08T06:30:00+00:00",
    ]


async def test_S05_london_weekly(client, ws):
    got = await _next(client, _row(ws, "s5", ["0 9 * * 1"], "Europe/London"), _utc(2027, 3, 20), n=2)
    assert got == ["2027-03-22T09:00:00+00:00", "2027-03-29T08:00:00+00:00"]


async def test_S06_pause_resume_roundtrip(client, ws):
    row = _row(ws, "s6", ["0 9 * * 1"], "UTC")
    assert await asch.upsert(row) == "created"
    try:
        await asch.pause(ws, "s6", "paused by Sam")
        d = await client.get_schedule_handle(asch.schedule_id(row)).describe()
        assert d.schedule.state.paused is True
        await asch.unpause(ws, "s6", "resumed")
        d = await client.get_schedule_handle(asch.schedule_id(row)).describe()
        assert d.schedule.state.paused is False
    finally:
        await asch.delete(ws, "s6")
    await asch.delete(ws, "s6")  # already gone is fine


async def test_S07_paused_schedule_reports_next_times(client, ws):
    # Recorded: a paused Schedule still reports next_action_times, so the confirm card can show the next
    # 3 runs of a draft (no "starts when turned on" fallback needed).
    row = _row(ws, "s7", ["0 9 * * 1"], "America/New_York", status="draft")
    await asch.upsert(row)
    try:
        runs = await asch.next_runs(ws, "s7")
        assert len(runs) == 3 and all(r.tzinfo is not None for r in runs)
    finally:
        await asch.delete(ws, "s7")


async def test_S08_one_off(client, ws):
    row = _row(ws, "s8", ["0 15 5 11 *"], "America/New_York", remainingRuns=1)
    await asch.upsert(row)
    try:
        d = await client.get_schedule_handle(asch.schedule_id(row)).describe()
        assert (d.schedule.state.limited_actions, d.schedule.state.remaining_actions) == (True, 1)
    finally:
        await asch.delete(ws, "s8")


async def test_upsert_is_idempotent_and_updates_on_change(client, ws):
    row = _row(ws, "s9", ["0 9 * * 1"], "UTC")
    try:
        assert await asch.upsert(row) == "created"
        assert await asch.upsert(row) is None
        assert await asch.upsert(row | {"cron": ["0 10 * * 1"]}) == "updated"
        assert await asch.upsert(row | {"cron": ["0 10 * * 1"], "status": "paused"}) == "updated"
        d = await client.get_schedule_handle(asch.schedule_id(row)).describe()
        assert d.schedule.state.paused is True
    finally:
        await asch.delete(ws, "s9")
