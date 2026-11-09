"""Bounded DEV-only native meeting-context receipt; synthetic source, never a provider meeting.

Run from apps/backend with ENVIRONMENT=dev AWS_PROFILE=isol8-admin and the dev Clerk issuer.
Requires an already-promoted DEV backend/image. One effects-off Router run, four iterations,
75-second model budget. No admission outbox, TaskWorkflow, Recall or phone operation is created.
Only IDs, boolean checks and usage are saved; signing grants and hook tokens stay in memory.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
import sqlite3
import tempfile
from pathlib import Path
import time
import uuid

import boto3
import httpx
from botocore.exceptions import ClientError
from temporalio.testing import ActivityEnvironment

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo, tasks_repo
from core.services import meeting_context, runtime_runs
from core.temporal import activities
from core.temporal.types import RouterTurnIn
from scripts.multiparty_e2e.runner import Clerk, _secret
from scripts.super_agent.acceptance.ctx import API_ROOT, Ctx, DEFAULT_WORKSPACE, guard


async def fixture(ctx: Ctx, label: str) -> dict:
    """Owned synthetic DEV source; no message dispatch and no admission pointer."""
    room = (
        (
            await ctx.api("owner").post(
                "/api/v1/messaging/rooms",
                json={
                    "clientRoomId": str(uuid.uuid4()),
                    "name": "synthetic-context-receipt-" + label + "-" + uuid.uuid4().hex[:6],
                    "kind": "channel",
                    "members": [],
                },
            )
        )
        .raise_for_status()
        .json()
    )
    room_id, occurrence = room["roomId"], int(time.time())
    marker = "Synthetic source marker " + uuid.uuid4().hex
    attachment = (
        (
            await ctx.api("owner").post(
                f"/api/v1/messaging/rooms/{room_id}/attachments",
                files={"image": ("synthetic-transcript.txt", marker.encode(), "text/plain")},
            )
        )
        .raise_for_status()
        .json()
    )
    snapshot = {
        "transcriptId": str(attachment["id"]),
        "transcriptRoomId": room_id,
        "inCallAt": occurrence,
        "endedAt": occurrence + 1,
    }
    await messaging_repo.patch_room_meta(
        room_id,
        {
            "meeting": {
                **snapshot,
                "startAt": occurrence,
                "status": "ended",
                "title": "SYNTHETIC validation; no meeting occurred",
            }
        },
    )
    request_id = "synthetic-context-receipt:" + str(uuid.uuid4())
    await messaging_repo.put_meeting_notes_request(
        ctx.ws,
        request_id,
        {
            "roomId": room_id,
            "occurrence": occurrence,
            "principalUserId": ctx.users["owner"],
            "meeting": snapshot,
        },
    )
    trigger_id = f"meeting-followups:{room_id}:{occurrence}"
    await tasks_repo.put_trigger(
        ctx.ws,
        {
            "triggerId": trigger_id,
            "source": "webhook",
            "admission": "candidate",
            "lane": "shared",
            "actorUserId": ctx.users["owner"],
            "receivedAt": utc_now_iso(),
            "ref": {
                "roomId": room_id,
                "origin": "meeting",
                "confirmAll": True,
                "meetingFollowups": {"requestId": request_id, "occurrence": occurrence, "meetingRoomId": room_id},
            },
        },
    )
    return {"roomId": room_id, "triggerId": trigger_id, "marker": marker}


async def native_receipt(owner: str, identity, grant: str, marker: str) -> dict:
    """Inspect only this run's profile snapshot/record; temporary SQLite bytes never survive the check."""
    s3 = boto3.client("s3", region_name="us-east-1")
    bucket = "lightbulb-dev-openclaw-replicas"  # same DEV-only source bucket as service-stack.ts
    prefix = f"{owner}/hermes/{identity.agent_id}/"

    def inspect():
        data = s3.get_object(Bucket=bucket, Key=prefix + "state.db")["Body"].read()
        record = s3.get_object(Bucket=bucket, Key=prefix + ".runs/" + identity.run_id + ".json")["Body"].read()
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
            tmp.write(data)
            tmp.flush()
            with sqlite3.connect(tmp.name) as db:
                rows = db.execute(
                    "SELECT id, role, content, tool_calls FROM messages WHERE session_id = ? ORDER BY id",
                    (identity.session_key,),
                ).fetchall()
        calls = []
        source_result = False
        for mid, role, content, raw_calls in rows:
            if role == "tool" and marker in str(content):
                source_result = True
            for call in json.loads(raw_calls or "[]"):
                function = call.get("function") or {}
                calls.append({"messageId": mid, "name": function.get("name")})
        ephemeral = s3.list_objects_v2(Bucket=bucket, Prefix=prefix + ".lightbulb-turn/", MaxKeys=1)
        return {
            "nativeToolCalls": calls,
            "nativeAllowedToolResult": source_result,
            "grantAbsentFromProfileDatabase": grant.encode() not in data,
            "grantAbsentFromDurableRunRecord": grant.encode() not in record,
            "ephemeralContextNotSynced": not ephemeral.get("Contents"),
        }

    for attempt in range(6):
        try:
            result = await run_in_thread(inspect)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404"}:
                raise
            if attempt == 5:
                raise RuntimeError("native receipt artifact still missing after bounded flush retries") from None
        else:
            if result["nativeAllowedToolResult"] or attempt == 5:
                return result
        if attempt < 5:
            await asyncio.sleep(5)
    raise AssertionError("unreachable")


async def validate(receipt_path: Path) -> None:
    guard()  # must precede every secret read, identity lookup, write and model invocation
    if DEFAULT_WORKSPACE != "org_3LgB9twuJbYCjGILfOL5JjgQV0p":
        raise RuntimeError("unexpected validation workspace")
    if not await meeting_context.enabled(DEFAULT_WORKSPACE):
        raise RuntimeError("followUps is disabled; refusing to change the owner's preference")
    # Existing DEV secrets, process only. Never print or persist these values.
    settings.CLERK_SECRET_KEY = _secret("lightbulb/dev/clerk_secret_key")
    os.environ["PAPERCLIP_SERVICE_TOKEN_KEY"] = _secret("lightbulb/dev/paperclip_service_token_key")
    clerk = Clerk()
    org = clerk.http.get(f"/organizations/{DEFAULT_WORKSPACE}").raise_for_status().json()
    ctx = Ctx(ws=DEFAULT_WORKSPACE, users={"owner": org["created_by"]}, clerk=clerk)
    receipt = {
        "ownerId": ctx.ws,
        "syntheticFixture": True,
        "providerMeetingCreated": False,
        "canonicalWorkTasksCreated": False,
        "routerRuns": 1,
        "maxIterations": 4,
        "modelBudgetSeconds": 75,
    }
    original_create = runtime_runs.create_run
    original_budget, original_iterations = activities.ROUTER_BUDGET_S, activities.ROUTER_MAX_ITERATIONS
    private = {}

    async def create(owner, identity, request):
        # The actual production _router_run already reauthorized stored batch IDs and minted this grant.
        # Only the diagnostic instructions/limits change; there is no ad-hoc grant construction.
        assert request.effects == "off" and request.context_grant
        private.update(grant=request.context_grant, identity=identity)
        request = replace(
            request,
            instructions=(
                "This is an authorized read-only deployment diagnostic. Call meeting_context exactly once for the "
                "trigger named in the input, section transcript. Then use reply for that same trigger with the exact "
                "source text returned. No other tools or effects, no task creation or provider contact. End Done."
            ),
            max_iterations=4,
            run_budget_seconds=75,
        )
        assert request.context_grant not in json.dumps(request.to_wire())
        return await original_create(owner, identity, request)

    try:
        allowed, forbidden = await fixture(ctx, "allowed"), await fixture(ctx, "private-other-batch")
        receipt.update(
            roomId=allowed["roomId"], triggerId=allowed["triggerId"], forbiddenTriggerId=forbidden["triggerId"]
        )
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        runtime_runs.create_run = create
        activities.ROUTER_BUDGET_S, activities.ROUTER_MAX_ITERATIONS = 75, 4
        inp = RouterTurnIn(ctx.ws, "shared", str(uuid.uuid4()), [allowed["triggerId"]])
        status = await ActivityEnvironment().run(
            activities._router_run,
            ctx.ws,
            inp,
            "Read original source for trigger " + allowed["triggerId"],
            {},
            allowed["roomId"],
            ctx.users["owner"],
        )
        identity, grant = private["identity"], private["grant"]
        receipt.update(runId=identity.run_id, sessionKey=identity.session_key, runStatus=status.status)
        reply = " ".join(d.get("text", "") for d in (status.decisions or []) if d.get("kind") == "reply")
        checks = {
            "modelReadAllowedSource": allowed["marker"] in reply,
            "otherPrivateSourceAbsent": forbidden["marker"] not in reply,
        }
        container = await runtime_runs._container(ctx.ws)
        headers = {"Authorization": "Bearer " + container["hook_token"], meeting_context.GRANT_HEADER: grant}
        url = f"{API_ROOT}/api/v1/internal/runtime/{ctx.ws}/meeting-context"
        async with httpx.AsyncClient(timeout=20) as client:
            for name, run_id, trigger_id, hdrs in (
                ("allowedHttp", identity.run_id, allowed["triggerId"], headers),
                ("sameOwnerOtherPrivateTrigger", identity.run_id, forbidden["triggerId"], headers),
                ("otherRouterRun", str(uuid.uuid4()), allowed["triggerId"], headers),
                ("missingGrant", identity.run_id, allowed["triggerId"], {"Authorization": headers["Authorization"]}),
            ):
                response = await client.post(
                    url, headers=hdrs, json={"runId": run_id, "triggerId": trigger_id, "section": "transcript"}
                )
                expected = 200 if name == "allowedHttp" else 403
                checks[name] = response.status_code == expected
                if name == "allowedHttp":
                    checks["allowedHttpExactSource"] = (
                        response.status_code == 200 and response.json().get("text") == allowed["marker"]
                    )
                else:
                    checks[name + "NoSourceLeak"] = (
                        allowed["marker"] not in response.text and forbidden["marker"] not in response.text
                    )
        native = await native_receipt(ctx.ws, identity, grant, allowed["marker"])
        receipt["nativeToolCalls"] = native.pop("nativeToolCalls")
        checks.update(native)
        checks["exactlyOneNativeContextRead"] = (
            sum(c["name"] == "meeting_context" for c in receipt["nativeToolCalls"]) == 1
        )
        checks["pollContainsNoGrant"] = grant not in json.dumps(status.__dict__, default=str)
        receipt["checks"], receipt["passed"] = checks, all(checks.values())
        if status.usage:
            receipt["usage"] = {
                "inputTokens": status.usage.get("input_tokens"),
                "outputTokens": status.usage.get("output_tokens"),
            }
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({"receipt": str(receipt_path), "passed": receipt["passed"], "runId": identity.run_id}))
        if not receipt["passed"]:
            raise RuntimeError("meeting-context diagnostic did not pass; inspect boolean receipt")
    finally:
        runtime_runs.create_run = original_create
        activities.ROUTER_BUDGET_S, activities.ROUTER_MAX_ITERATIONS = original_budget, original_iterations
        private.clear()
        os.environ.pop("PAPERCLIP_SERVICE_TOKEN_KEY", None)
        await ctx.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(validate(args.receipt))
