"""H4.2a — Hermes transport smoke harness.

Drives ONE chat turn against a booted Hermes container by calling the runtime
port's ``send_chat`` DIRECTLY — no WebSocket, no ``/chat`` UI, no control-plane
plumbing. This is the de-risking step of the Hermes live spike: it proves the
container boots in Fargate, the ``POST /v1/runs`` → SSE ``GET /v1/runs/{id}/events``
transport streams through our networking, ``_translate_event`` produces the
internal ChatEvent shape, the agent acts on the wake-text, and the terminal
``ChatResult`` (+ in-adapter usage metering) lands — all WITHOUT the H3b bridge.

Prerequisites (the hands-on H4 steps, see the runbook):
  1. The Hermes extended image is published to ECR ``isol8/hermes-extended``.
  2. A dev owner has been switched to ``runtime_kind="hermes"``
     (``POST /api/v1/debug/runtime-kind``) and provisioned, so a Hermes
     container is RUNNING with an ``API_SERVER_KEY`` on its row.

Run it with the backend's environment — easiest is inside the backend task via
ECS exec (it already has AWS creds + settings), e.g.::

    aws ecs execute-command --cluster "$CLUSTER" --task "$BACKEND_TASK" \
      --container backend --interactive --profile isol8-admin --region us-east-1 \
      --command "/bin/sh -c 'cd /app && uv run python -m scripts.hermes_transport_smoke \
        --owner-id org_xxx --message \"Say hello and tell me your name.\"'"

Locally it needs the same env the backend loads (AWS profile + the ECS/EFS
settings) and network reachability to the container's private IP, so the
ECS-exec path is the reliable one.

Exit status: 0 only if the run reached a terminal ``completed`` status AND at
least one ``chunk`` event streamed (i.e. the agent actually produced output).
Anything else exits non-zero so the harness doubles as a CI-style gate.
"""

import argparse
import asyncio
import logging
import sys
from collections import Counter

logger = logging.getLogger("hermes_transport_smoke")

# Cap how much of each event we echo so a long answer doesn't flood the terminal.
_ECHO_CHARS = 200


def _summarize_event(event: dict) -> str:
    """One-line, length-capped rendering of a translated ChatEvent."""
    etype = event.get("type", "?")
    if etype == "chunk":
        text = event.get("content") or ""
        return f"chunk        {text[:_ECHO_CHARS]!r}"
    if etype in ("tool_start", "tool_end"):
        return f"{etype:<12} tool={event.get('tool')!r}"
    if etype == "error":
        return f"error        {event.get('message')!r}"
    if etype == "done":
        return "done"
    # Unknown/other event shapes: dump a trimmed repr so nothing is hidden.
    return f"{etype:<12} {str(event)[:_ECHO_CHARS]}"


def _pick_agent(roster: list[dict], agent_id: str | None) -> tuple[str, str]:
    """Resolve which agent to drive: explicit ``--agent-id`` or the CEO default.

    Mirrors ``build_agents_list_response`` — the ``ceo`` (Friday) is the default,
    else the first roster entry. Returns ``(agent_id, human_label)``.
    """
    if agent_id:
        return agent_id, agent_id
    if not roster:
        raise SystemExit(
            "no roster and no --agent-id: is the owner provisioned with a "
            "Paperclip company (Friday/Atlas seeded)? Pass --agent-id to override."
        )
    ceo = next((a for a in roster if a.get("role") == "ceo"), None)
    chosen = ceo or roster[0]
    aid = chosen.get("id")
    if not aid:
        raise SystemExit(f"roster entry has no id: {chosen!r}")
    label = chosen.get("name") or chosen.get("role") or aid
    return aid, f"{label} ({aid})"


async def main(owner_id: str, agent_id: str | None, message: str, session_id: str | None) -> int:
    # Import inside main so ``--help`` works without the backend's AWS/settings
    # import chain (mirrors the other scripts' import-lightness).
    from core.runtime import get_runtime_for_owner, prime_runtime_kind
    from core.runtime.hermes.chat_bridge import fetch_hermes_roster

    # 1. Warm the process-local runtime-kind cache off the owner's row, then
    #    resolve the runtime. A non-hermes owner means the dev switch never ran.
    kind = await prime_runtime_kind(owner_id)
    runtime = get_runtime_for_owner(owner_id)
    print(f"[resolve]    owner={owner_id} runtime_kind={kind!r}")
    if runtime.kind != "hermes":
        print(
            f"[abort]      owner resolves to {runtime.kind!r}, not 'hermes'. "
            f"Run POST /api/v1/debug/runtime-kind (kind=hermes) then reprovision first."
        )
        return 2

    # 2. Resolve which agent to drive (explicit or CEO default from the roster).
    roster = await fetch_hermes_roster(owner_id)
    agent_id, agent_label = _pick_agent(roster, agent_id)
    session_key = f"agent:{agent_id}:{owner_id}" + (f":{session_id}" if session_id else "")
    print(f"[agent]      {agent_label}")
    print(f"[session]    {session_key}")
    print(f"[message]    {message!r}")
    print("[stream]     ---- translated ChatEvents ----")

    # 3. Drive the turn. The sink just echoes each translated event; the adapter
    #    meters usage internally on the terminal frame (nothing to do here).
    events: list[dict] = []

    async def sink(event: dict) -> None:
        events.append(event)
        print(f"             {_summarize_event(event)}")

    try:
        result = await runtime.send_chat(owner_id, agent_id, message, session_key, sink)
    except Exception as exc:  # noqa: BLE001 — the harness reports any failure verbatim.
        print(f"[error]      send_chat raised: {type(exc).__name__}: {exc}")
        return 1

    # 4. Report. ``result`` is None only if the adapter returned no terminal
    #    frame (treated as a failure for the gate).
    counts = Counter(e.get("type", "?") for e in events)
    print("[stream]     ---- end ----")
    print(f"[summary]    {len(events)} events; by type: {dict(counts)}")
    if result is None:
        print("[result]     no terminal ChatResult (stream produced none) — FAIL")
        return 1
    print(f"[result]     run_id={result.run_id} status={result.status!r}")

    ok = result.status == "completed" and counts.get("chunk", 0) > 0
    if not ok:
        print(
            "[verdict]    FAIL — expected terminal status 'completed' with >=1 chunk. "
            "If it wedged on an approval prompt, check POST /v1/runs/{id}/approval "
            "field name; if no chunks, check the SSE event vocabulary vs _translate_event."
        )
        return 1
    print("[verdict]    PASS — transport + SSE translation + agent output all confirmed.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="H4.2a Hermes transport smoke — drive one chat turn through the port.",
    )
    parser.add_argument(
        "--owner-id", required=True, help="Dev owner already switched to runtime_kind=hermes and provisioned."
    )
    parser.add_argument("--agent-id", default=None, help="Agent to drive (default: the CEO / Friday from the roster).")
    parser.add_argument(
        "--message",
        default="Hello — please say hi back and tell me your name in one sentence.",
        help="The wake-text to send.",
    )
    parser.add_argument("--session-id", default=None, help="Optional session id appended to the session key.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(
        asyncio.run(
            main(
                owner_id=args.owner_id,
                agent_id=args.agent_id,
                message=args.message,
                session_id=args.session_id,
            )
        )
    )
