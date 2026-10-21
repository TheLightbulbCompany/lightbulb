"""A task run posts by room id only to a room its task names.

Regression for prod 2026-10-19 04:48Z: Forge's ISOA-860 run (filed before From sections existed,
so its description named no room) "looped Prasiddha in" by posting into group DM e362794c, quiet
since Oct 7, which it picked from its own recall. send_room_message and lightbulb-post.sh both let
a Paperclip run post to any room the agent sat in."""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from bridge import server, turn

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

STALE_DM = "e362794c-da41-5894-a772-626d28838aa1"
FROM_ROOM = "ed0016e3-58c8-5f51-8abe-9847ef2c9a71"
SESSION = "paperclip:company:company-1:agent:agent-1:issue:issue-1"
IDENTITY = {"runId": "run-1", "companyId": "company-1", "agentId": "agent-1", "sessionId": SESSION,
            "issueId": "issue-1", "paperclipApiUrl": "http://paperclip.internal:3100"}
# The task mentions the stale room outside its From section: a mention is not a place to post.
FRESH = {"input": f"# Task\nFix pushes. Earlier context was in room {STALE_DM} (`{STALE_DM}`).\n\n"
                  f"## From\nFiled from room `{FROM_ROOM.upper()}`. The message that asked: "
                  f"/chat?panel=chat&room={FROM_ROOM}&message=4b800e3f-b3cd-4e19-977e-783a15c40a12",
         "instructions": f"Your notes mention {STALE_DM}.", "session_id": SESSION}


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def resolve_resume_session_id(self, key):
        return key

    def get_messages_as_conversation(self, sid):
        return self.rows


def test_task_rooms_are_only_the_from_section_room(monkeypatch):
    monkeypatch.setattr(turn, "_session_db", lambda agent_id: None)
    assert turn.task_rooms("agent-1", SESSION, FRESH) == [FROM_ROOM]
    assert turn.task_rooms("agent-1", SESSION, {"input": f"Post to {STALE_DM}.\n## From\nsomewhere"}) == []
    # A resumed session's prompt has no description; its first wake, in the session, still does.
    resumed = {"input": "New comment: ship it.", "instructions": "", "session_id": SESSION}
    rows = [{"role": "user", "content": FRESH["input"]},
            {"role": "user", "content": f"Comment: also see room {STALE_DM}."},
            {"role": "assistant", "content": f"## From\nFiled from room `{STALE_DM}`."}]
    monkeypatch.setattr(turn, "_session_db", lambda agent_id: _Db(rows))
    assert turn.task_rooms("agent-1", SESSION, resumed) == [FROM_ROOM]


def test_a_paperclip_run_hands_its_task_rooms_to_both_post_doors(home, monkeypatch):
    (home / "profiles" / "agent-1").mkdir(parents=True)
    monkeypatch.setattr(turn, "_session_db", lambda agent_id: None)
    created = []

    class Thread:
        def __init__(self, *a, target=None, args=(), **k):
            self.target, self.args = target, args
            created.append(self)

        def start(self):
            pass

    monkeypatch.setattr(server, "threading", type("M", (), {"Thread": Thread}))
    for name in ("restore_profile", "save_profile", "report_usage"):
        monkeypatch.setattr(turn, name, lambda *a, **k: None)
    for name in ("restore_root_auth", "save_root_auth", "ensure_profile"):
        monkeypatch.setattr(turn.sync, name, lambda *a, **k: None)
    seen = {}
    real_scope = turn._secret_scope

    def scope(agent_id, overlay=None):
        if overlay and turn.POST_ROOMS_ENV in overlay:
            seen["env"] = overlay[turn.POST_ROOMS_ENV]
        return real_scope(agent_id, overlay)

    monkeypatch.setattr(turn, "_secret_scope", scope)

    class Agent:
        model, session_input_tokens, session_output_tokens = "m", 0, 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kwargs):
            path = turn._turn_context_path(turn._profile_dir("agent-1"), SESSION)
            seen["ctx"] = json.loads(path.read_text())
            return {"final_response": "ok", "failed": False, "completed": True, "api_calls": 1}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: Agent())
    body = {"identity": IDENTITY, "request": FRESH}
    assert server.handle_plugin("/internal/paperclip/v1/runs", "POST", {"authorization": "Bearer gw-test"}, body)[0] == 202
    created[-1].target(*created[-1].args)
    assert seen["ctx"]["postRooms"] == [FROM_ROOM]
    assert seen["env"] == f"rooms:{FROM_ROOM}"


def test_send_room_message_refuses_a_room_the_task_does_not_name(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, {"apiUrl": "https://api.invalid", "ownerId": "org_1", "hookToken": "h",
                                           "agentId": "agent-1", "runId": "run-1", "sessionKey": SESSION,
                                           "roomId": None, "family": "paperclip", "postRooms": [FROM_ROOM]})
    sent = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: sent.append(body) or {"messageId": "m"})
    assert lightbulb._send_room_message({"roomId": STALE_DM, "text": "Prasiddha, an update"},
                                        session_id=SESSION) == lightbulb.TASK_ROOM_REFUSAL
    assert sent == []
    lightbulb._send_room_message({"roomId": FROM_ROOM, "text": "Fixed."}, session_id=SESSION)
    assert [b["roomId"] for b in sent] == [FROM_ROOM]


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "skills-src/lightbulb/scripts/lightbulb-post.sh"


@pytest.mark.skipif(not shutil.which("jq"), reason="the script needs jq")
@pytest.mark.parametrize("target,rooms,posted", [
    (STALE_DM, f"rooms:{FROM_ROOM}", False),
    (STALE_DM, "rooms:", False),
    (FROM_ROOM.upper(), f"rooms:{FROM_ROOM}", True),
    ("#report-bugs", "rooms:", True),       # a channel by name stays open
    (STALE_DM, None, True),                  # not a task run: unchanged
    (STALE_DM, "", True),                    # the process-wide empty placeholder is not a task run
])
def test_lightbulb_post_refuses_a_room_id_the_task_does_not_name(tmp_path, target, rooms, posted):
    log = tmp_path / "curl.json"
    fake = tmp_path / "bin" / "curl"
    fake.parent.mkdir()
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'while [[ $# -gt 0 ]]; do case "$1" in -d) printf "%s" "$2" > "$LOG"; shift 2;; *) url="$1"; shift;; esac; done\n'
        'if [[ "$url" == */receipts/* ]]; then printf 404; exit 0; fi\n'
        "printf '{\"roomId\":\"r1\"}\\n201'\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}", "LOG": str(log), "PAPERCLIP_AGENT_ID": "agent-1",
           "OPENCLAW_HOOK_TOKEN": "t", "ISOL8_API_URL": "https://api.x/", "ISOL8_OWNER_ID": "org_1"}
    env.pop("LIGHTBULB_POST_ROOMS", None)
    if rooms is not None:
        env["LIGHTBULB_POST_ROOMS"] = rooms
    proc = subprocess.run(["bash", str(SCRIPT), "--room", target], input="Prasiddha, an update", env=env,
                          capture_output=True, text=True)
    assert (proc.returncode == 0) is posted, proc.stderr
    assert log.exists() is posted
    if not posted:
        assert "does not name room" in proc.stderr and "--user" in proc.stderr

