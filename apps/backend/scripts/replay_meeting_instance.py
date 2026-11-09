"""Replay one received recurring-instance invite through the normal business transition.

Default is read-only. Run with deployed backend configuration. --execute processes the
original calendar occurrence, never creates a Google Calendar event or edits its parent.
"""

from __future__ import annotations

import argparse
import asyncio
import email
import json
import time
from email.policy import default
from datetime import datetime

import boto3
import dkim

from core.config import settings
from core.repositories import agent_email_repo, messaging_repo
from core.services import clerk_admin, meeting_invites as mi
from core.services.agent_mail_inbound import authenticated_sender, parse_from_address


def authenticated_mime(raw: bytes, authorized_google_sender: str | None = None):
    """Verify signatures cryptographically; never trust Authentication-Results headers."""
    msg = email.message_from_bytes(raw, policy=default)
    sender = parse_from_address(str(msg.get("From", "")))
    domains = []
    verifier = dkim.DKIM(raw, timeout=5)
    signatures = [value for name, value in verifier.headers if name.lower() == b"dkim-signature"]
    for i, signature in enumerate(signatures):
        if dkim.DKIM(raw, timeout=5).verify(i):
            tags = dkim.util.parse_tag_value(signature)
            domains.append(tags.get(b"d", b"").decode())
    # Calendar sends for an organizer under its own verified Google signature. This
    # operator-only exception is explicit and still requires current member/organizer,
    # exact stored series and original recipient below; it never changes inbound auth.
    google_authorized = authorized_google_sender == sender and "google.com" in domains
    if not google_authorized and not authenticated_sender({"dkim": "PASS", "dkim_domains": domains}, {}, sender):
        raise ValueError("No verified From-aligned DKIM signature")
    calendars = [p.get_content() for p in msg.walk() if p.get_content_type() == "text/calendar"]
    if len(calendars) != 1:
        raise ValueError("Expected exactly one calendar part")
    invite = mi.parse(calendars[0])
    return msg, sender, invite


def validate(invite, *, parent_uid: str, url: str, now: float, expected_start=None, expected_end=None):
    if not invite or invite.method != "REQUEST" or invite.instance_of != parent_uid or invite.recurrence:
        raise ValueError("Expected an individual REQUEST override of the specified series")
    if invite.meeting_url != url or not invite.start or not invite.end or invite.end.timestamp() <= now:
        raise ValueError("Meeting URL mismatch or occurrence already ended")
    if expected_start is not None and invite.start != expected_start:
        raise ValueError("Occurrence start does not match operator-authorized time")
    if expected_end is not None and invite.end != expected_end:
        raise ValueError("Occurrence end does not match operator-authorized time")
    if invite.end.timestamp() - invite.start.timestamp() > 86400:
        raise ValueError("Occurrence duration exceeds one day")


async def replay(args):
    raw = await asyncio.to_thread(
        lambda: boto3.client("s3").get_object(Bucket=args.bucket, Key=args.key)["Body"].read()
    )
    msg, sender, invite = await asyncio.to_thread(authenticated_mime, raw, args.authorized_google_sender)
    validate(
        invite,
        parent_uid=args.parent_uid,
        url=args.meet_url,
        now=time.time(),
        expected_start=datetime.fromisoformat(args.expected_start),
        expected_end=datetime.fromisoformat(args.expected_end),
    )
    team = await agent_email_repo.resolve(args.address)
    if not team or team.get("owner_id") != args.owner:
        raise ValueError("Recipient does not belong to expected workspace")
    recipients = email.utils.getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))
    if args.address.lower() not in [a.lower() for _, a in recipients]:
        raise ValueError("Expected recipient absent from original MIME")
    members = await clerk_admin.list_organization_members(args.owner, strict=True)
    creator = next((m["user_id"] for m in members if str(m.get("email", "")).lower() == sender), None)
    if not creator or invite.organizer_email != sender:
        raise ValueError("Authenticated sender must be current workspace member and organizer")
    parent_room = messaging_repo.meeting_room_id(args.owner, args.parent_uid)
    parent = await messaging_repo.get_room(parent_room)
    before = (parent or {}).get("meeting") or {}
    if (
        not parent
        or parent.get("ownerId") != args.owner
        or before.get("uid") != args.parent_uid
        or before.get("meetingUrl") != args.meet_url
    ):
        raise ValueError("Parent room/owner/UID/URL mismatch")
    if not before.get("recurrence") or int(before.get("startAt", 0)) <= invite.end.timestamp():
        raise ValueError("Parent must be the future recurring occurrence, separate from this override")
    room_id = messaging_repo.meeting_room_id(args.owner, invite.uid)
    sem = asyncio.Semaphore(12)

    async def load(row):
        async with sem:
            return await messaging_repo.get_room(row["roomId"])

    async def duplicates():
        rooms = await messaging_repo.list_owner_rooms(args.owner)
        for room in await asyncio.gather(*(load(r) for r in rooms)):
            m = (room or {}).get("meeting") or {}
            if (
                m.get("meetingUrl") == args.meet_url
                and m.get("recallBotId")
                and m.get("status") in ("bot_created", "in_call")
                and int(m.get("startAt", 0)) < invite.end.timestamp()
                and int(m.get("endAt", 0)) > invite.start.timestamp()
            ):
                return {
                    "room": room.get("roomId"),
                    "bot": m["recallBotId"],
                    "status": m["status"],
                    "startAt": str(m.get("startAt")),
                }
        return None

    receipt = {
        "owner": args.owner,
        "source": args.key,
        "childRoom": room_id,
        "childUid": invite.uid,
        "start": invite.start.isoformat(),
        "end": invite.end.isoformat(),
        "parentRoom": parent_room,
        "parentStart": str(before["startAt"]),
        "parentBot": before.get("recallBotId"),
    }
    if not args.execute:
        receipt.update(mode="read_only", existing=await duplicates())
        return receipt
    async with mi.meeting_lease(room_id):
        validate(
            invite,
            parent_uid=args.parent_uid,
            url=args.meet_url,
            now=time.time(),
            expected_start=datetime.fromisoformat(args.expected_start),
            expected_end=datetime.fromisoformat(args.expected_end),
        )
        existing = await duplicates()
        if existing:
            receipt.update(mode="already_booked", existing=existing)
            return receipt
        receipt["outcome"] = await mi._apply(args.owner, invite, room_id, creator, team, sender)
    after = ((await messaging_repo.get_room(parent_room)) or {}).get("meeting") or {}
    if after.get("startAt") != before.get("startAt") or after.get("recallBotId") != before.get("recallBotId"):
        raise RuntimeError("Parent changed concurrently; inspect before retrying")
    child = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
    receipt.update(
        mode="replayed",
        child={k: str(child.get(k)) for k in ("status", "recallBotId", "startAt", "endAt", "sequence")},
        parentUnchanged=True,
    )
    return receipt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("owner", "bucket", "key", "address", "parent-uid", "meet-url", "expected-start", "expected-end"):
        p.add_argument("--" + name, required=True)
    p.add_argument(
        "--authorized-google-sender",
        help="Operator-authorized organizer; requires cryptographically verified google.com DKIM",
    )
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    if settings.ENVIRONMENT not in ("dev", "prod"):
        p.error("Load deployed backend configuration first")
    print(json.dumps(asyncio.run(replay(args))))


if __name__ == "__main__":
    main()
