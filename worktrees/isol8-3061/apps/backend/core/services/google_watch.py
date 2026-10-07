"""Google push channels (Calendar ``events.watch`` / Drive ``changes.watch``),
driven through the owner's gog keyring — the Google-side half of the
``auto_google_watch`` trigger shape (see ``inbound_triggers``).

Google has no long-lived webhook registration: a watch opens a CHANNEL that
expires (≤7 days), POSTs notifications carrying no payload beyond headers,
and authenticates only by echoing back the token set at watch time
(``X-Goog-Channel-Token``). The seed sets that token to the trigger's own
secret so Paperclip's native ``bearer`` mode verifies deliveries (via the
trigger proxy's header mapping); the routine's own agent re-watches with a
fresh channel id (same token) before expiry, driven by the maintenance
schedule trigger and EFS state file the seed leaves behind (see
``inbound_triggers``) — there is no platform-side renewal job.

gog invocations mirror the ``gog_cli`` materializer exactly: fixed argv
(caller data rides as discrete argv elements, never a shell string),
``_gog_env`` for the owner-scoped XDG keyring environment, the shared
``_GOG_TIMEOUT_S`` cap, and ``asyncio.to_thread`` so the subprocess never
blocks the event loop. The account email is validated against the stored
inventory by the seed before it ever reaches an argv.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
import uuid

from core.services.credentials.materializers.gog_cli import _GOG_TIMEOUT_S, _gog_env

logger = logging.getLogger(__name__)

# Both watch surfaces accept up to 7 days. The ACTUAL grant comes back in the
# response's `expiration` and is what gets stored, so a Google-side clamp just
# means the routine's maintenance tick re-arms that channel sooner.
_CHANNEL_TTL_SECONDS = 7 * 24 * 60 * 60

# The registry's curated event ids for the google provider — the dispatch keys
# for which watch surface to open.
CALENDAR_EVENT = "calendar.changed"
DRIVE_EVENT = "drive.changed"


def _run_gog(argv: list[str], env: dict[str, str]) -> str:
    """Run one gog command and return its stdout.

    A gog refusal (broken grant, revoked account, Google API error) raises
    ``ValueError`` with the last stderr line — the seed maps ValueError to a
    caller-facing 400 instead of an opaque 500.
    """
    try:
        proc = subprocess.run(
            argv,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GOG_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        suffix = f": {detail[-1][:200]}" if detail else ""
        raise ValueError(f"Google refused the watch request{suffix}") from exc
    return proc.stdout


def _parse_channel(stdout: str) -> tuple[str, int]:
    """``(resource_id, expiration_ms)`` from a watch response — Google's
    Channel resource, which gog prints as JSON. Both fields are required to
    ever stop or renew the channel, so absence is as fatal as the call
    failing."""
    data = json.loads(stdout)
    resource_id = data.get("resourceId") if isinstance(data, dict) else None
    expiration = data.get("expiration") if isinstance(data, dict) else None
    if not resource_id or not expiration:
        raise RuntimeError("gog watch returned no resourceId/expiration")
    return resource_id, int(expiration)


def _start_watch_sync(
    owner_id: str, account_email: str, provider_event: str, proxy_url: str, channel_token: str
) -> dict:
    env = _gog_env(owner_id)
    channel_id = str(uuid.uuid4())

    if provider_event == DRIVE_EVENT:
        # changes.watch needs a start pageToken — fetched fresh so the channel
        # reports changes from "now", not from some historical token.
        token_out = _run_gog(["gog", "drive", "changes", "start-token", "-a", account_email], env)
        page_token = json.loads(token_out).get("startPageToken")
        if not page_token:
            raise RuntimeError("gog start-token returned no startPageToken")
        expiration_target = int(time.time() * 1000) + _CHANNEL_TTL_SECONDS * 1000
        out = _run_gog(
            [
                "gog",
                "drive",
                "changes",
                "watch",
                f"--token={page_token}",
                f"--webhook-url={proxy_url}",
                f"--channel-id={channel_id}",
                f"--channel-token={channel_token}",
                f"--expiration-ms={expiration_target}",
                "-a",
                account_email,
            ],
            env,
        )
        resource_id, expiration_ms = _parse_channel(out)
        return {
            "channel_id": channel_id,
            "resource_id": resource_id,
            "expiration_ms": expiration_ms,
            "page_token": page_token,
        }

    if provider_event == CALENDAR_EVENT:
        # gog has no dedicated calendar-watch command, so this rides the
        # generic Discovery caller against the account's primary calendar.
        # ttl is Calendar's channel-lifetime knob, in seconds, as a string.
        body = json.dumps(
            {
                "id": channel_id,
                "type": "web_hook",
                "address": proxy_url,
                "token": channel_token,
                "params": {"ttl": str(_CHANNEL_TTL_SECONDS)},
            }
        )
        out = _run_gog(
            [
                "gog",
                "api",
                "call",
                "calendar",
                "v3",
                "events.watch",
                "--params",
                '{"calendarId":"primary"}',
                "--body",
                body,
                "--allow-write",
                "--force",
                "-a",
                account_email,
            ],
            env,
        )
        resource_id, expiration_ms = _parse_channel(out)
        return {"channel_id": channel_id, "resource_id": resource_id, "expiration_ms": expiration_ms}

    raise ValueError(f"unknown Google watch event {provider_event!r}")


async def start_watch(
    *, owner_id: str, account_email: str, provider_event: str, proxy_url: str, channel_token: str
) -> dict:
    """Open a push channel for the event kind, pointed at ``proxy_url`` and
    authenticated by ``channel_token`` (the trigger's own secret).

    Returns ``{channel_id, resource_id, expiration_ms[, page_token]}`` —
    everything the recorded channel entry needs to stop or renew it later.
    """
    return await asyncio.to_thread(_start_watch_sync, owner_id, account_email, provider_event, proxy_url, channel_token)


def _stop_watch_sync(owner_id: str, account_email: str, provider_event: str, channel_id: str, resource_id: str) -> None:
    env = _gog_env(owner_id)
    if provider_event == DRIVE_EVENT:
        _run_gog(["gog", "drive", "changes", "stop", channel_id, resource_id, "-a", account_email], env)
        return
    body = json.dumps({"id": channel_id, "resourceId": resource_id})
    _run_gog(
        [
            "gog",
            "api",
            "call",
            "calendar",
            "v3",
            "channels.stop",
            "--body",
            body,
            "--allow-write",
            "--force",
            "-a",
            account_email,
        ],
        env,
    )


async def stop_watch(
    *, owner_id: str, account_email: str, provider_event: str, channel_id: str, resource_id: str
) -> None:
    """Best-effort close of a channel being replaced or rolled back — never
    masks the caller's own error (same posture as ``_delete_vercel_webhook``);
    an unstopped channel dies at its ≤7-day expiry anyway."""
    try:
        await asyncio.to_thread(_stop_watch_sync, owner_id, account_email, provider_event, channel_id, resource_id)
    except Exception:
        logger.exception("google_watch: stop of channel %s failed", channel_id)


# --- Gmail watch (users.watch → the OWNER'S Pub/Sub topic) -------------------
#
# Unlike the push channels above, a Gmail watch has no channel id / resource
# id: users.watch binds ONE mailbox to ONE topic (a re-call replaces the
# previous binding), notifications arrive as Pub/Sub push (OIDC-signed, no
# X-Goog-Channel-Token), and renewal is `gog gmail watch renew` — run by the
# routine's own agent in-container (the same gog keyring), which re-arms from
# the topic/labels gog stored at start time.


def _parse_gmail_watch(stdout: str) -> int:
    """``expiration_ms`` from a users.watch response (gog prints the API's
    JSON: historyId + expiration in ms). Without it the seeded state file
    could never say when to re-arm, so absence is as fatal as the call
    failing."""
    data = json.loads(stdout)
    expiration = data.get("expiration") if isinstance(data, dict) else None
    if not expiration:
        raise RuntimeError("gog gmail watch returned no expiration")
    return int(expiration)


def _start_gmail_watch_sync(owner_id: str, account_email: str, topic: str) -> dict:
    env = _gog_env(owner_id)
    # INBOX only — a full-mailbox watch fires on sent/archived/label churn,
    # and "new email" is the curated event. The topic is validated by the
    # seed's regex fence before it ever reaches this argv.
    out = _run_gog(
        ["gog", "gmail", "watch", "start", "--topic", topic, "--label-ids", "INBOX", "-a", account_email],
        env,
    )
    return {"expiration_ms": _parse_gmail_watch(out)}


async def start_gmail_watch(*, owner_id: str, account_email: str, topic: str) -> dict:
    """Register the mailbox to the owner's Pub/Sub ``topic``. Returns
    ``{expiration_ms}`` — all the recorded watch entry needs to renew it."""
    return await asyncio.to_thread(_start_gmail_watch_sync, owner_id, account_email, topic)


def _stop_gmail_watch_sync(owner_id: str, account_email: str) -> None:
    env = _gog_env(owner_id)
    _run_gog(["gog", "gmail", "watch", "stop", "-a", account_email], env)


async def stop_gmail_watch(*, owner_id: str, account_email: str) -> None:
    """Best-effort stop of a Gmail watch being rolled back — same posture as
    ``stop_watch``; an unstopped watch dies at its ~7-day expiry anyway."""
    try:
        await asyncio.to_thread(_stop_gmail_watch_sync, owner_id, account_email)
    except Exception:
        logger.exception("google_watch: gmail watch stop failed for %s", account_email)
