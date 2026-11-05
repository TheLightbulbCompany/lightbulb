"""The watchdog's only control surface (spec 11 §3.3, F3: live changes are `WatchdogConfig` only).

    uv run python -m scripts.watchdog_ctl --show
    uv run python -m scripts.watchdog_ctl --set judge_per_tick=5 --by user_admin
    uv run python -m scripts.watchdog_ctl --off --by user_admin      # ticks continue, nothing is read or written
    uv run python -m scripts.watchdog_ctl --pause|--resume --by user_admin
    uv run python -m scripts.watchdog_ctl --report
    uv run python -m scripts.watchdog_ctl --label 20261104#org_x#<task>#C1#1 tp --by user_admin [--note "..."]

Every change needs a platform admin (`PLATFORM_ADMIN_USER_IDS`) and writes an admin audit row. Refuses when
the super-agent env master is off (prod).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import typing

from core.config import settings
from core.repositories import admin_actions_repo, watchdog_repo
from core.services import watchdog
from core.services.super_agent_flags import env_enabled
from core.temporal.watchdog_types import WatchdogConfig

OFF = {"reconcile": "off", "tier1": "off", "tier2": "off", "judge": False}


class CtlError(Exception):
    pass


def parse_set(pairs: list[str]) -> dict:
    """`k=v` pairs checked against `WatchdogConfig`'s field types (ints, floats, bools, the mode literals)."""
    hints = typing.get_type_hints(WatchdogConfig)
    out: dict = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        if key not in hints:
            raise CtlError(f"unknown setting {key!r}")
        kind = hints[key]
        if typing.get_origin(kind) is typing.Literal:
            if raw not in typing.get_args(kind):
                raise CtlError(f"{key} must be one of {typing.get_args(kind)}")
            out[key] = raw
        elif kind is bool:
            if raw not in ("true", "false"):
                raise CtlError(f"{key} must be true or false")
            out[key] = raw == "true"
        else:
            try:
                value = kind(raw)
            except ValueError as exc:
                raise CtlError(f"{key} must be a {kind.__name__}") from exc
            if value < 0:
                raise CtlError(f"{key} must not be negative")
            out[key] = value
    return out


def _admin(by: str | None) -> str:
    if not by or by not in settings.platform_admin_user_ids:
        raise CtlError("--by must name a platform admin")
    return by


async def _audit(by: str, action: str, payload: dict) -> None:
    await admin_actions_repo.create(
        admin_user_id=by,
        target_user_id="__watchdog__",
        action=action,
        payload=payload,
        result="ok",
        audit_status="written",
        http_status=200,
        elapsed_ms=0,
        error_message=None,
        user_agent="scripts.watchdog_ctl",
        ip="",
    )


async def _config(client, handle) -> WatchdogConfig:
    desc = await handle.describe()
    raw = desc.schedule.action.args
    return (await client.data_converter.decode(raw, [WatchdogConfig]))[0] if raw else WatchdogConfig()


async def _update_config(client, handle, changes: dict) -> tuple[WatchdogConfig, WatchdogConfig]:
    old = await _config(client, handle)
    new = dataclasses.replace(old, **changes)

    def updater(inp):
        from temporalio.client import ScheduleUpdate

        inp.description.schedule.action.args = [new]
        return ScheduleUpdate(schedule=inp.description.schedule)

    await handle.update(updater)
    return old, new


async def run(args: argparse.Namespace, client=None) -> dict:
    if not env_enabled():
        raise CtlError("the super-agent env master is off here; the watchdog does not run")
    if args.label:
        fid, label = args.label
        if label not in ("tp", "fp", "unsure"):
            raise CtlError("label must be tp, fp or unsure")
        by = _admin(args.by)
        row = await watchdog_repo.label(*watchdog_repo.finding_key(fid), label, by, args.note)
        await _audit(by, "watchdog.label", {"findingId": fid, "label": label})
        return {"findingId": fid, "label": row["label"], "labelledBy": row["labelledBy"]}
    if args.report:
        return await watchdog.report()
    if client is None:
        from core.temporal.client import connect

        client = await connect()
    handle = client.get_schedule_handle(watchdog.SCHEDULE_ID)
    if args.show:
        desc = await handle.describe()
        return {"config": dataclasses.asdict(await _config(client, handle)), "paused": desc.schedule.state.paused}
    by = _admin(args.by)
    if args.pause or args.resume:
        await (handle.pause(note=f"by {by}") if args.pause else handle.unpause(note=f"by {by}"))
        await _audit(by, "watchdog.pause" if args.pause else "watchdog.resume", {})
        return {"paused": bool(args.pause)}
    changes = OFF if args.off else parse_set(args.set or [])
    if not changes:
        raise CtlError("nothing to do")
    old, new = await _update_config(client, handle, changes)
    await _audit(by, "watchdog.config", {"old": dataclasses.asdict(old), "new": dataclasses.asdict(new)})
    return {"config": dataclasses.asdict(new)}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="watchdog_ctl")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true")
    g.add_argument("--set", nargs="+", metavar="K=V")
    g.add_argument("--off", action="store_true")
    g.add_argument("--pause", action="store_true")
    g.add_argument("--resume", action="store_true")
    g.add_argument("--report", action="store_true")
    g.add_argument("--label", nargs=2, metavar=("FINDING_ID", "tp|fp|unsure"))
    p.add_argument("--by", help="platform admin user id (every change)")
    p.add_argument("--note")
    return p


def main() -> int:
    try:
        print(json.dumps(asyncio.run(run(parser().parse_args())), indent=2))
    except CtlError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
