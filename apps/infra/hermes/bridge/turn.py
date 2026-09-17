"""One Hermes turn: profile restore, run the agent, post run/reply callbacks to the backend."""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import urllib.request

from bridge import API, HOOK_TOKEN, MODEL, OWNER, now
from bridge.sync import restore_profile, save_profile, _profile_dir

log = logging.getLogger("bridge")

_busy = 0
_busy_lock = threading.Lock()
_runs: dict[str, str] = {}  # runId -> state, for replay detection within this process
_last_turn: dict[str, float] = {}


def _busy_add(n: int) -> None:
    global _busy
    with _busy_lock:
        _busy += n


# ---- backend callbacks (hook token) -----------------------------------------------------------
def _post_backend(path: str, body: dict) -> int:
    req = urllib.request.Request(
        f"{API}/api/v1/internal/messaging/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {HOOK_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        log.warning("backend %s -> %s %s", path, exc.code, exc.read()[:200])
        return exc.code
    except Exception as exc:  # noqa: BLE001
        log.warning("backend %s failed: %s", path, exc)
        return 0


# ---- the turn --------------------------------------------------------------------------------
def _history(body: dict, agent_id: str) -> list[dict]:
    """The room's recent messages as Hermes conversation history: this agent's own messages are
    the assistant, everything else is a user turn prefixed with the author's name."""
    names = {p["id"]: (p.get("displayName") or p.get("name") or p["id"]) for p in body.get("participants", [])}
    out = []
    for m in body.get("recentHistory", []):
        author = m.get("author") or {}
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if author.get("kind") == "agent" and author.get("id") == agent_id:
            out.append({"role": "assistant", "content": text})
        else:
            out.append({"role": "user", "content": f"{names.get(author.get('id'), author.get('id', '?'))}: {text}"})
    return out


def run_turn(agent_id: str, body: dict) -> None:
    run = body.get("run") or {}
    run_id = run.get("runId")
    room = body.get("room") or {}
    msg = body.get("message") or {}
    session_key = f"agent:{agent_id}:lightbulb:{room.get('kind', 'channel')}:{room.get('id')}"
    _busy_add(1)
    outcome, error_kind, reply = "completed", None, ""
    usage = {"model": MODEL, "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0}
    t0 = time.time()
    done = threading.Event()

    def _heartbeat() -> None:  # the backend sweeper closes a run silent for 90 s
        while not done.wait(30):
            _post_backend(f"runs/{OWNER}/events", {"runId": run_id, "type": "run.heartbeat", "at": now(), "sessionKey": session_key})

    try:
        restore_profile(agent_id)
        _post_backend(f"runs/{OWNER}/events", {"runId": run_id, "type": "run.started", "at": now(), "sessionKey": session_key})
        threading.Thread(target=_heartbeat, name=f"hb-{run_id}", daemon=True).start()
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(_profile_dir(agent_id))
        try:
            from run_agent import AIAgent

            sender = body.get("sender") or {}
            agent = AIAgent(
                model=MODEL,
                provider="bedrock",
                quiet_mode=True,
                session_id=f"{room.get('id')}:{agent_id}",
                platform="lightbulb",
                user_id=str(sender.get("id", "")),
                chat_id=str(room.get("id", "")),
                chat_type="group" if room.get("kind") != "dm" else "dm",
            )
            me = next((p for p in body.get("participants", []) if p.get("id") == agent_id), {})
            system = (
                f"You are {me.get('displayName') or me.get('name') or 'an agent'} in the Lightbulb room "
                f"#{room.get('name') or room.get('id')}. Reply as yourself; other participants are humans and agents."
            )
            result = agent.run_conversation(
                user_message=f"{(body.get('sender') or {}).get('id', '?')}: {msg.get('text', '')}",
                system_message=system,
                conversation_history=_history(body, agent_id) or None,
            )
            reply = (result or {}).get("final_response") or ""
            usage.update(
                inputTokens=int(getattr(agent, "session_input_tokens", 0) or 0),
                outputTokens=int(getattr(agent, "session_output_tokens", 0) or 0),
                cacheReadTokens=int(getattr(agent, "session_cache_read_tokens", 0) or 0),
                cacheWriteTokens=int(getattr(agent, "session_cache_write_tokens", 0) or 0),
            )
            if not (result or {}).get("api_calls"):  # Hermes answers a dead model with apologetic prose; never post it
                outcome, error_kind, reply = "error", "model_unavailable", ""
            elif not reply:
                outcome, error_kind = "error", "empty_response"
        finally:
            reset_hermes_home_override(token)
    except Exception as exc:  # noqa: BLE001
        outcome, error_kind = "error", type(exc).__name__
        log.error("turn %s failed: %s\n%s", run_id, exc, traceback.format_exc())
    done.set()
    took = time.time() - t0
    if reply:
        _post_backend(
            f"agent-messages/{OWNER}",
            {
                "eventId": f"{body.get('eventId')}:{agent_id}:reply",  # one ingress eventId fans out to N agents
                "roomId": room.get("id"),
                "agentId": agent_id,
                "text": reply,
                "replyToMessageId": None,
                "threadRootMessageId": None,
                "runId": run_id,
            },
        )
    _post_backend(
        f"runs/{OWNER}/events",
        {"runId": run_id, "type": "run.ended", "at": now(), "sessionKey": session_key, "outcome": outcome, "errorKind": error_kind, "usage": usage},
    )
    try:
        save_profile(agent_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("save_profile failed: %s", exc)
    _runs[run_id] = outcome
    _last_turn[agent_id] = took
    _busy_add(-1)
    log.info("turn %s agent=%s outcome=%s %.1fs reply=%r", run_id, agent_id, outcome, took, reply[:80])
