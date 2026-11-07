"""The dev backend side of the meeting self-test (run.py). Runs in the BACKEND's venv, from apps/backend, over the
dev messaging table (AWS profile isol8-admin):

    uv run --project ../backend python ../huddle-agent/tests/meet_sim/dev.py seed <tag>   -> dispatch metadata
    uv run --project ../backend python ../huddle-agent/tests/meet_sim/dev.py record <roomId>  -> META.meeting + truth

`seed` makes a hidden meeting room in the "Super Agent Test" workspace the way lane M did (02-meet-dev.md): an
in-call meeting whose roster has the host member p1 and two guests, p2 and p3. `record` returns what the worker
left on the meeting plus the ground truth the answers are scored against: the same call counts and bookings the
read tools return, computed now.
"""

import asyncio
import json
import os
import sys
import time
import uuid

for k, v in {"CLERK_ISSUER": "https://x.clerk.accounts.dev", "ENCRYPTION_KEY": "wHc3hAOcLlFzWyu3Ph7xIyClIdVQTrIzFOZDtu_pIEY=",
             "NETWORK_PROOF_KEY": "x", "GATE_BEARER_HMAC_KEY": "x", "AWS_REGION": "us-east-1",
             "AWS_DEFAULT_REGION": "us-east-1", "AWS_PROFILE": "isol8-admin"}.items():  # fmt: skip
    os.environ.setdefault(k, v)  # dummies: only the messaging table and the worker secret are used
sys.path.insert(0, os.getcwd())

from core.config import settings  # noqa: E402

settings.MESSAGING_TABLE = "lightbulb-dev-messaging"
from core.repositories import messaging_repo  # noqa: E402
from core.services import livekit_huddles  # noqa: E402
from routers import voice_meet, voice_phone  # noqa: E402

OWNER, USER = "org_3LgB9twuJbYCjGILfOL5JjgQV0p", "user_3LgB4LPRZ1MOhpg5wSfGrH5jMcA"
EMAIL = "sa-test+clerk_test@thelightbulb.company"
ROSTER = {"p1": ("Maya Chen", True), "p2": ("Leo Park", False), "p3": ("Alex Guest", False)}


async def seed(tag: str) -> dict:
    room_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-meet-sim:{tag}"))
    now = int(time.time())
    meeting = {
        "uid": f"meet-sim-{tag}", "sequence": 0, "title": "Monday check-in (self-test)",
        "startAt": now, "endAt": now + 1800, "status": "in_call", "inCallAt": now,
        "organizer": EMAIL, "invitedBy": EMAIL,
        "roster": {p: {"name": n, "at": now * 1000, "in": True, "host": h} for p, (n, h) in ROSTER.items()},
    }  # fmt: skip
    members = [{"kind": "human", "id": USER, "displayName": "SA Test", "mentionHandle": None, "role": "owner",
                "listenMode": None}]  # fmt: skip
    if await messaging_repo.get_room(room_id) is None:
        await messaging_repo.create_room(
            owner_id=OWNER, room_id=room_id, client_room_id=f"meeting:meet-sim-{tag}", creator_user_id=USER,
            name="Monday check-in (self-test)", kind="channel", members=members,
            req_hash=messaging_repo.request_hash("meet-sim", tag), extra={"meeting": meeting, "hidden": True},
        )  # fmt: skip
    settings.HUDDLE_WORKER_SECRET = os.environ["HUDDLE_WORKER_SECRET"]
    return {
        "dispatch": {"roomId": room_id, "ownerId": OWNER, "roomToken": livekit_huddles.room_token(room_id, OWNER),
                     "source": "meet", "company": "Super Agent Test"},
        "name": await voice_meet.assistant_name(OWNER),
        "roster": {p: n for p, (n, _) in ROSTER.items()},
    }  # fmt: skip


async def record(room_id: str) -> dict:
    meta = await messaging_repo.get_room(room_id) or {}
    m = meta.get("meeting") or {}
    return {
        "meeting": {k: m.get(k) for k in ("asked", "followUps", "summary", "transcriptAttachedAt", "status")},
        "truth": {
            "calls_today": await voice_meet._calls(OWNER, 1),
            "calls_week": await voice_meet._calls(OWNER, 7),
            "bookings": [b["sk"].removeprefix("VOICEBOOK#") for b in await voice_phone._bookings(OWNER, "")],
        },
    }


if __name__ == "__main__":
    op, arg = sys.argv[1], sys.argv[2]
    print(json.dumps(asyncio.run(seed(arg) if op == "seed" else record(arg)), default=str))
