"""A task's work runs in the task's own Paperclip session, as stock Paperclip does (owner ruling
2026-10-18, reversing 2026-10-04's issue -> room conversation routing, which let a room turn that
never recorded a disposition get the task blocked: ISOA-860). A room turn files the task with a
From section; the task's own run reports done / needs-you back to that room."""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

from bridge import turn

AGENT = "agent-1"
ROOM = "11111111-1111-4111-8111-111111111111"
ROOM_KEY = f"agent:{AGENT}:lightbulb:channel:{ROOM}"
ISSUE = "33333333-3333-4333-8333-333333333333"
RUN = "pc-run-1"
SESSION = f"paperclip:company:company-1:agent:{AGENT}:issue:{ISSUE}"
IDENTITY = {"runId": RUN, "companyId": "company-1", "agentId": AGENT, "sessionId": SESSION,
            "issueId": ISSUE, "paperclipApiUrl": "http://paperclip.internal:3100"}
REQUEST = {"input": "Wake: issue assigned", "instructions": "Follow the wake.", "session_id": SESSION}
SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "skills-src/paperclip/scripts/paperclip-api.sh"


def _old_binding():
    """What a 2026-10-04-era helper left on disk: it must change nothing now."""
    path = turn._profile_dir(AGENT) / "lightbulb-issue-bindings" / f"{ISSUE}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessionKey": ROOM_KEY, "boundAt": 9e9}))


# ---- the bridge -------------------------------------------------------------------------------
def test_a_paperclip_run_runs_in_the_issue_session_even_with_an_old_binding_on_disk(monkeypatch):
    _old_binding()
    seen = {}
    monkeypatch.setattr(turn, "_start_desktop_turn", lambda _p, ctx: seen.update(desktop=ctx["sessionKey"]))
    monkeypatch.setattr(turn, "_run_native_paperclip", lambda *args: (seen.update(args=args), ("completed", None, "ok", {}))[1])
    assert turn._run_paperclip_session(IDENTITY, REQUEST)[0] == "completed"
    assert seen == {"desktop": SESSION, "args": (IDENTITY, REQUEST)}


def test_the_native_run_uses_its_own_session_and_has_no_room_for_prompts(monkeypatch):
    _old_binding()
    seen = {"prompts": []}
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)

    class Agent:
        model = ""
        session_input_tokens = session_output_tokens = session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **kw):
            seen["run"] = kw
            return {"api_calls": 1, "final_response": "ok"}

    def construct(agent_id, body, frames, session_key):
        seen["construct"] = (body, session_key)
        return Agent()

    monkeypatch.setattr(turn, "_construct_agent", construct)
    real_scope = turn.website_logins.prompt_scope
    monkeypatch.setattr(turn.website_logins, "prompt_scope",
                        lambda agent_id, room_id, thread_root, aborted: (seen["prompts"].append(room_id), real_scope(agent_id, room_id, thread_root, aborted))[1])
    turn.register_paperclip_run(IDENTITY, REQUEST)
    assert turn._run_native_paperclip(IDENTITY, REQUEST)[0] == "completed"
    assert seen["construct"] == ({"sessionKey": SESSION}, SESSION)
    assert seen["run"]["user_message"] == REQUEST["input"] and "conversation_history" not in seen["run"]
    assert seen["prompts"] == [None]


# ---- the helper -------------------------------------------------------------------------------
def _helper(tmp_path, args, *, session_key=ROOM_KEY, room_key=None, message_id=None, stdin="",
            responses=None, ok=True, extra_env=None):
    """Run paperclip-api.sh against a fake curl that records each request. `responses` is one
    [status, body] per request, in order (the last repeats)."""
    calls, count = tmp_path / "calls.jsonl", tmp_path / "count"
    calls.unlink(missing_ok=True)
    count.write_text("0")
    fake = tmp_path / "bin" / "curl"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'out=""; method=""; body=""; headers=(); while [[ $# -gt 0 ]]; do case "$1" in -o) out="$2"; shift 2;;'
        ' -H) headers+=("$2"); shift 2;; -X) method="$2"; shift 2;; --data-binary) body="$2"; shift 2;;'
        ' -w) shift 2;; -sS) shift;; *) url="$1"; shift;; esac; done\n'
        'n=$(<"$COUNT"); echo $((n + 1)) > "$COUNT"\n'
        'jq -nc --arg m "$method" --arg url "$url" --arg body "$body" --args'
        ' \'{method: $m, url: $url, body: $body, headers: $ARGS.positional}\' "${headers[@]}" >> "$CALLS"\n'
        'jq -c --argjson n "$n" \'.[if $n < length then $n else length - 1 end]\' <<<"$RESPONSES" > "$out.pair"\n'
        'jq -c ".[1]" "$out.pair" > "$out"; jq -r ".[0]" "$out.pair"; rm -f "$out.pair"\n'
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}", "CALLS": str(calls), "COUNT": str(count),
           "PAPERCLIP_API_URL": "http://pc", "PAPERCLIP_API_KEY": "k", "PAPERCLIP_AGENT_ID": AGENT,
           "HERMES_HOME": str(tmp_path / "profile"), "HERMES_SESSION_KEY": session_key,
           "RESPONSES": json.dumps(responses or [[201, {"id": ISSUE, "status": "todo"}]])}
    for name in ("PAPERCLIP_RUN_ID", "LIGHTBULB_ROOM_SESSION_KEY", "LIGHTBULB_ROOM_MESSAGE_ID",
                 "LIGHTBULB_ROOM_MESSAGE_TEXT", "LIGHTBULB_ROOM_MESSAGE_AUTHOR"):
        env.pop(name, None)
    if room_key:
        env["LIGHTBULB_ROOM_SESSION_KEY"] = room_key
    if message_id:
        env["LIGHTBULB_ROOM_MESSAGE_ID"] = message_id
    env.update(extra_env or {})
    proc = subprocess.run(["bash", str(SCRIPT), *args], input=stdin, env=env, capture_output=True, text=True)
    assert (proc.returncode == 0) is ok, proc.stderr
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


helper = pytest.mark.skipif(not shutil.which("jq"), reason="the helper needs jq")
FROM = f"## From\nFiled from room `{ROOM}`."


@helper
def test_an_assigned_create_is_one_post_carrying_todo(tmp_path):
    [create] = _helper(tmp_path, ["create", "company-1", "--title", "Fix it", "--assignee", AGENT])
    body = json.loads(create["body"])
    assert create["method"] == "POST" and body["status"] == "todo" and body["assigneeAgentId"] == AGENT
    assert not (tmp_path / "profile" / "lightbulb-issue-bindings").exists()
    assert not any(h.startswith("X-Paperclip-Run-Id") for h in create["headers"])


@helper
@pytest.mark.parametrize("command", [["create", "company-1"], ["subtask", "company-1", "--parent", ISSUE]])
def test_a_room_create_appends_one_from_section_after_the_description(tmp_path, command):
    [create] = _helper(tmp_path, [*command, "--title", "Fix it", "--assignee", AGENT],
                       stdin="Asked by Ana: fix the 500.", message_id="m-1")
    assert json.loads(create["body"])["description"] == (
        f"Asked by Ana: fix the 500.\n\n{FROM} The message that asked: "
        f"/chat?panel=chat&room={ROOM}&message=m-1")


@helper
def test_the_asking_message_is_quoted_into_the_from_section(tmp_path):
    [create] = _helper(tmp_path, ["create", "company-1", "--title", "Fix it", "--assignee", AGENT],
                       extra_env={"LIGHTBULB_ROOM_MESSAGE_TEXT": "Refund Santi.\nFull amount.",
                                  "LIGHTBULB_ROOM_MESSAGE_AUTHOR": "Aden Kim"})
    assert json.loads(create["body"])["description"] == f"{FROM}\n\nAden Kim wrote:\n> Refund Santi.\n> Full amount."


@helper
def test_a_from_section_is_never_appended_twice_and_names_the_room_beside_a_call(tmp_path):
    [create] = _helper(tmp_path, ["create", "company-1", "--title", "Fix it"],
                       stdin=f"Context.\n\n{FROM}")
    assert json.loads(create["body"])["description"].count("## From") == 1
    call = turn.call_session_key(AGENT, "call-1")
    [create] = _helper(tmp_path, ["create", "company-1", "--title", "Fix it"], session_key=call, room_key=ROOM_KEY)
    assert json.loads(create["body"])["description"] == FROM  # no message id: no link, never an invented one


@helper
def test_a_create_outside_a_room_session_appends_nothing(tmp_path):
    [create] = _helper(tmp_path, ["create", "company-1", "--title", "Fix it", "--assignee", AGENT],
                       session_key=f"agent:{AGENT}:{SESSION}", stdin="Context.")
    assert json.loads(create["body"])["description"] == "Context."
    [create] = _helper(tmp_path, ["create", "company-1", "--title", "Fix it"], session_key="")
    assert "description" not in json.loads(create["body"])


@helper
def test_create_can_file_into_a_project_and_assign_a_person(tmp_path):
    calls = _helper(tmp_path, ["create", "company-1", "--title", "Sign the form", "--project", "proj-1",
                               "--assignee-user", "user-9"], session_key="")
    body = json.loads(calls[0]["body"])
    assert body["projectId"] == "proj-1" and body["assigneeUserId"] == "user-9"
    assert "assigneeAgentId" not in body and body["status"] == "todo"


@helper
def test_subtask_can_file_into_a_project(tmp_path):
    calls = _helper(tmp_path, ["subtask", "company-1", "--title", "One directory", "--parent", ISSUE,
                               "--project", "proj-1", "--assignee-user", "user-9"], session_key="")
    body = json.loads(calls[0]["body"])
    assert body["projectId"] == "proj-1" and body["assigneeUserId"] == "user-9" and body["status"] == "todo"


@helper
def test_subtask_in_a_project_with_an_agent_assignee_starts_it(tmp_path):
    [create] = _helper(tmp_path, ["subtask", "company-1", "--title", "One directory", "--parent", ISSUE,
                                  "--project", "proj-1", "--assignee", AGENT, "--status", "todo"], session_key="")
    body = json.loads(create["body"])
    assert body["projectId"] == "proj-1" and body["assigneeAgentId"] == AGENT and body["status"] == "todo"
