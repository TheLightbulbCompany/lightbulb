"""One conversation per piece of work: Paperclip's run for an issue a room conversation owns lands
in that conversation (ISOA-680: a reconciler run redid a room turn's fix and opened duplicate PR
#3712). The helper records the binding; the bridge steers the wake into the live room turn, or
runs it as a turn of that conversation, or -- with no usable binding -- in the issue session."""
import json
import os
import pathlib
import shutil
import subprocess
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

from bridge import turn

AGENT = "agent-1"
ROOM = "11111111-1111-4111-8111-111111111111"
ROOM_KEY = f"agent:{AGENT}:lightbulb:channel:{ROOM}"
ISSUE = "33333333-3333-4333-8333-333333333333"
RUN = "pc-run-1"
ROOM_RUN = "22222222-2222-4222-8222-222222222222"
SESSION = f"paperclip:company:company-1:agent:{AGENT}:issue:{ISSUE}"
IDENTITY = {"runId": RUN, "companyId": "company-1", "agentId": AGENT, "sessionId": SESSION,
            "issueId": ISSUE, "paperclipApiUrl": "http://paperclip.internal:3100"}
REQUEST = {"input": "Wake: issue assigned", "instructions": "Follow the wake.", "session_id": SESSION}
SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "skills-src/paperclip/scripts/paperclip-api.sh"


def _bind(key=ROOM_KEY, issue=ISSUE, bound_at=None):
    path = turn._issue_binding_path(AGENT, issue)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessionKey": key, "boundAt": time.time() if bound_at is None else bound_at}))
    return path


# ---- the helper records the binding and carries the attached run id ---------------------------
def _helper(tmp_path, args, *, session_key=ROOM_KEY, responses=None, run_id=None, stdin="", ok=True):
    """Run paperclip-api.sh against a fake curl. `responses` is one [status, body] per request, in
    order (the last repeats). Each recorded call says whether ISSUE's binding existed WHEN that
    request was sent -- the property that keeps a wake from overtaking the binding."""
    calls, count = tmp_path / "calls.jsonl", tmp_path / "count"
    count.write_text("0")
    fake = tmp_path / "bin" / "curl"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'out=""; method=""; body=""; headers=(); while [[ $# -gt 0 ]]; do case "$1" in -o) out="$2"; shift 2;;'
        ' -H) headers+=("$2"); shift 2;; -X) method="$2"; shift 2;; --data-binary) body="$2"; shift 2;;'
        ' -w) shift 2;; -sS) shift;; *) url="$1"; shift;; esac; done\n'
        'n=$(<"$COUNT"); echo $((n + 1)) > "$COUNT"\n'
        'bound=false; [[ -e "$BINDING" ]] && bound=true\n'
        'jq -nc --arg m "$method" --arg url "$url" --arg body "$body" --argjson bound "$bound" --args'
        ' \'{method: $m, url: $url, body: $body, bound: $bound, headers: $ARGS.positional}\' "${headers[@]}" >> "$CALLS"\n'
        'jq -c --argjson n "$n" \'.[if $n < length then $n else length - 1 end]\' <<<"$RESPONSES" > "$out.pair"\n'
        'jq -c ".[1]" "$out.pair" > "$out"; jq -r ".[0]" "$out.pair"; rm -f "$out.pair"\n'
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}", "CALLS": str(calls), "COUNT": str(count),
           "BINDING": str(tmp_path / "profile" / "lightbulb-issue-bindings" / f"{ISSUE}.json"),
           "PAPERCLIP_API_URL": "http://pc", "PAPERCLIP_API_KEY": "k", "PAPERCLIP_AGENT_ID": AGENT,
           "HERMES_HOME": str(tmp_path / "profile"), "HERMES_SESSION_KEY": session_key,
           "RESPONSES": json.dumps(responses or [[200, {"id": ISSUE, "status": "in_progress"}]])}
    env.pop("PAPERCLIP_RUN_ID", None)
    if run_id:
        env["PAPERCLIP_RUN_ID"] = run_id
    if calls.exists():
        calls.unlink()
    proc = subprocess.run(["bash", str(SCRIPT), *args], input=stdin, env=env, capture_output=True, text=True)
    assert (proc.returncode == 0) is ok, proc.stderr
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


helper = pytest.mark.skipif(not shutil.which("jq"), reason="the helper needs jq")


def _binding(tmp_path):
    return tmp_path / "profile" / "lightbulb-issue-bindings" / f"{ISSUE}.json"


@helper
def test_an_assigned_active_create_is_filed_as_backlog_bound_and_only_then_started(tmp_path):
    calls = _helper(tmp_path, ["create", "company-1", "--title", "Fix it", "--assignee", AGENT, "--status", "todo"],
                    responses=[[201, {"id": ISSUE, "status": "backlog"}], [200, {"id": ISSUE, "status": "todo"}]])
    create, start = calls
    assert create["method"] == "POST" and json.loads(create["body"])["status"] == "backlog"  # backlog never wakes
    assert start == {**start, "method": "PATCH", "url": f"http://pc/api/issues/{ISSUE}", "bound": True}
    assert json.loads(start["body"]) == {"status": "todo"}
    binding = json.loads(_binding(tmp_path).read_text())
    assert binding["sessionKey"] == ROOM_KEY and abs(binding["boundAt"] - time.time()) < 60


@helper
def test_a_create_that_cannot_wake_anyone_is_sent_as_asked(tmp_path):
    calls = _helper(tmp_path, ["create", "company-1", "--title", "Later", "--assignee", AGENT, "--status", "backlog"],
                    responses=[[201, {"id": ISSUE, "status": "backlog"}]])
    assert [c["method"] for c in calls] == ["POST"] and _binding(tmp_path).exists()


@helper
def test_a_failed_start_after_the_backlog_create_removes_the_binding(tmp_path):
    _helper(tmp_path, ["create", "company-1", "--title", "Fix it", "--assignee", AGENT, "--status", "in_progress"],
            responses=[[201, {"id": ISSUE, "status": "backlog"}], [422, {"error": "no"}]], ok=False)
    assert not _binding(tmp_path).exists()


@helper
def test_an_update_binds_before_it_is_sent_and_unbinds_if_it_fails(tmp_path):
    calls = _helper(tmp_path, ["update", ISSUE, "--status", "in_progress"])
    assert calls[0]["method"] == "PATCH" and calls[0]["bound"] is True
    _binding(tmp_path).unlink()
    _helper(tmp_path, ["update", ISSUE, "--status", "in_progress"], responses=[[409, {"error": "locked"}]], ok=False)
    assert not _binding(tmp_path).exists()
    _bind_file = _binding(tmp_path)
    _bind_file.parent.mkdir(parents=True, exist_ok=True)
    _bind_file.write_text(json.dumps({"sessionKey": ROOM_KEY, "boundAt": 1}))
    _helper(tmp_path, ["update", ISSUE, "--title", "x"], responses=[[409, {"error": "locked"}]], ok=False)
    assert _bind_file.exists()  # a binding that predates the failed write stays


@helper
def test_an_update_by_identifier_resolves_the_id_with_a_read_first(tmp_path):
    calls = _helper(tmp_path, ["update", "ISOA-680", "--assignee", AGENT],
                    responses=[[200, {"id": ISSUE, "status": "todo"}]])
    read, write = calls
    assert (read["method"], read["url"]) == ("GET", "http://pc/api/issues/ISOA-680")
    assert write["method"] == "PATCH" and write["bound"] is True


@helper
def test_a_checkout_binds_before_it_is_sent(tmp_path):
    calls = _helper(tmp_path, ["checkout", ISSUE])
    assert calls[0]["url"].endswith("/checkout") and calls[0]["bound"] is True


@helper
def test_done_unbinds_and_only_a_room_conversation_binds_and_a_comment_never_does(tmp_path):
    _helper(tmp_path, ["update", ISSUE, "--status", "done"], responses=[[200, {"id": ISSUE, "status": "done"}]])
    assert not _binding(tmp_path).exists()
    calls = _helper(tmp_path, ["update", ISSUE, "--status", "in_progress"], session_key=f"agent:{AGENT}:{SESSION}")
    assert calls[0]["bound"] is False and not _binding(tmp_path).exists()
    calls = _helper(tmp_path, ["comment", ISSUE], responses=[[201, {"id": "comment-1"}]], stdin="on it")
    assert calls and not _binding(tmp_path).exists()


@helper
def test_writes_to_an_issue_carry_the_run_the_bridge_attached_and_no_other(tmp_path):
    attached = tmp_path / "profile" / ".lightbulb-turn" / f"paperclip-run.{ISSUE}"
    attached.parent.mkdir(parents=True)
    attached.write_text(RUN)
    calls = _helper(tmp_path, ["update", ISSUE, "--status", "in_progress"])
    assert f"X-Paperclip-Run-Id: {RUN}" in calls[-1]["headers"]
    other = "44444444-4444-4444-8444-444444444444"
    calls = _helper(tmp_path, ["update", other, "--status", "in_progress"], responses=[[200, {"id": other, "status": "todo"}]])
    assert not any(h.startswith("X-Paperclip-Run-Id") for h in calls[-1]["headers"])
    calls = _helper(tmp_path, ["update", ISSUE, "--title", "x"], run_id="own-run")  # a real run keeps its own
    assert "X-Paperclip-Run-Id: own-run" in calls[-1]["headers"]


# ---- the bridge reads the binding --------------------------------------------------------------
def test_a_binding_must_name_this_agents_own_room_conversation_and_be_fresh():
    assert turn.issue_conversation(AGENT, ISSUE) is None
    path = _bind()
    assert turn.issue_conversation(AGENT, ISSUE) == ROOM_KEY
    _bind(key=f"agent:other:lightbulb:channel:{ROOM}")
    assert turn.issue_conversation(AGENT, ISSUE) is None
    _bind(key=SESSION)
    assert turn.issue_conversation(AGENT, ISSUE) is None
    assert turn.issue_conversation(AGENT, "../x") is None


def test_a_binding_ages_by_its_bound_at_not_the_file_mtime_a_restore_resets():
    path = _bind(bound_at=time.time() - turn.ISSUE_BINDING_TTL_S - 60)
    os.utime(path, None)  # a fresh restore: brand-new mtime
    assert turn.issue_conversation(AGENT, ISSUE) is None and not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessionKey": ROOM_KEY}))  # no boundAt: not the helper's
    assert turn.issue_conversation(AGENT, ISSUE) is None and not path.exists()


# ---- the bridge routes Paperclip's run ---------------------------------------------------------
@pytest.fixture
def routed(monkeypatch):
    seen = {}

    def session(identity, request, conversation):
        seen["conversation"] = conversation
        seen["attached"] = (turn._profile_dir(AGENT) / turn.TURN_CONTEXT_DIRNAME / f"paperclip-run.{ISSUE}").exists()
        return "completed", None, "done", dict(turn.ZERO_USAGE)

    monkeypatch.setattr(turn, "_run_paperclip_session", session)
    turn.register_paperclip_run(IDENTITY, REQUEST)
    return seen


def test_no_binding_runs_in_the_issue_session(routed):
    assert turn._route_paperclip_run(IDENTITY, REQUEST)[0] == "completed"
    assert routed == {"conversation": None, "attached": False}


def test_a_bound_issue_with_no_live_turn_runs_in_the_room_conversation(routed, monkeypatch):
    _bind()
    monkeypatch.setattr(turn, "_room_member", lambda key: True)
    turn._route_paperclip_run(IDENTITY, REQUEST)
    assert routed == {"conversation": ROOM_KEY, "attached": True}
    assert not (turn._profile_dir(AGENT) / turn.TURN_CONTEXT_DIRNAME / f"paperclip-run.{ISSUE}").exists()


def test_a_room_the_agent_left_drops_the_binding_and_an_unknown_answer_keeps_it(routed, monkeypatch):
    path = _bind()
    monkeypatch.setattr(turn, "_room_member", lambda key: None)
    turn._route_paperclip_run(IDENTITY, REQUEST)
    assert routed == {"conversation": None, "attached": False} and path.exists()
    monkeypatch.setattr(turn, "_room_member", lambda key: False)
    turn._route_paperclip_run(IDENTITY, REQUEST)
    assert routed["conversation"] is None and not path.exists()


@pytest.mark.parametrize(("status", "answer"), [(404, False), (409, False), (401, None), (403, None),
                                                 (400, None), (500, None), (503, None)])
def test_only_the_endpoints_own_no_is_definite(monkeypatch, status, answer):
    asked = []

    def get(path):
        asked.append(path)
        raise urllib.error.HTTPError(path, status, "x", {}, None)

    monkeypatch.setattr(turn.sync, "_get_backend_bytes", get)
    assert turn._room_member(ROOM_KEY) is answer
    assert asked == [f"rooms/{turn.OWNER}/{ROOM}/agents/{AGENT}"]


def test_room_membership_yes_and_network_failure(monkeypatch):
    monkeypatch.setattr(turn.sync, "_get_backend_bytes", lambda path: b"")
    assert turn._room_member(ROOM_KEY) is True
    monkeypatch.setattr(turn.sync, "_get_backend_bytes", lambda path: (_ for _ in ()).throw(OSError("down")))
    assert turn._room_member(ROOM_KEY) is None


def test_a_rejected_hook_token_keeps_the_binding_and_uses_the_issue_session(routed, monkeypatch):
    path = _bind()
    monkeypatch.setattr(turn.sync, "_get_backend_bytes",
                        lambda p: (_ for _ in ()).throw(urllib.error.HTTPError(p, 401, "x", {}, None)))
    turn._route_paperclip_run(IDENTITY, REQUEST)
    assert routed == {"conversation": None, "attached": False} and path.exists()


def _live_room_turn():
    turn.register_run(ROOM_RUN)
    turn.RUNS[ROOM_RUN]["sessionKey"] = ROOM_KEY
    agent = SimpleNamespace(seen=[])
    agent.steer = lambda text: (agent.seen.append(text), True)[1]
    turn.attach_agent(ROOM_RUN, agent)
    return agent


def test_a_live_room_turn_takes_the_wake_and_holds_the_paperclip_run_open(routed, monkeypatch):
    _bind()
    monkeypatch.setattr(turn, "ROOM_RUN_POLL_S", 0.01)
    monkeypatch.setattr(turn, "_room_member", lambda key: pytest.fail("a live turn proves membership"))
    agent = _live_room_turn()
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("r", turn._route_paperclip_run(IDENTITY, REQUEST)))
    worker.start()
    attached = turn._profile_dir(AGENT) / turn.TURN_CONTEXT_DIRNAME / f"paperclip-run.{ISSUE}"
    for _ in range(200):
        if agent.seen:
            break
        time.sleep(0.01)
    assert agent.seen and RUN in agent.seen[0] and REQUEST["input"] in agent.seen[0]
    assert worker.is_alive() and attached.read_text() == RUN
    assert turn.paperclip_run_status(RUN, IDENTITY)["status"] == "running"
    turn.finish_run(ROOM_RUN, "completed")
    worker.join(2)
    outcome, error, output, usage = result["r"]
    assert (outcome, error, usage) == ("completed", None, turn.ZERO_USAGE) and ROOM_RUN in output
    assert routed == {} and not attached.exists()  # no session of its own ran


def test_stopping_the_paperclip_run_never_stops_the_room_turn(routed, monkeypatch):
    _bind()
    monkeypatch.setattr(turn, "ROOM_RUN_POLL_S", 0.01)
    _live_room_turn()
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("r", turn._route_paperclip_run(IDENTITY, REQUEST)))
    worker.start()
    for _ in range(200):
        if turn.RUNS[RUN]["state"] == "running":
            break
        time.sleep(0.01)
    assert turn.abort_run(RUN)
    worker.join(2)
    assert result["r"][0] == "cancelled" and turn.RUNS[ROOM_RUN]["state"] == "running"


def test_a_stopped_room_turn_cancels_the_attached_run_and_a_failed_one_fails_it(monkeypatch):
    monkeypatch.setattr(turn, "ROOM_RUN_POLL_S", 0.01)
    for room_outcome, expected in (("aborted", "cancelled"), ("error", "failed")):
        turn.RUNS.clear()
        turn.register_paperclip_run(IDENTITY, REQUEST)
        turn.register_run(ROOM_RUN)
        turn.finish_run(ROOM_RUN, room_outcome)
        assert turn._await_room_run(RUN, ROOM_RUN)[0] == expected


def test_a_turn_in_the_room_conversation_uses_its_lock_transcript_and_folder(monkeypatch):
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    monkeypatch.setattr(turn, "_room_history", lambda agent: ([{"role": "user", "content": "fix the bug"}], None))
    seen = {}

    class Agent:
        model = ""
        session_input_tokens = session_output_tokens = session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **kw):
            seen["run"] = kw
            seen["system"] = self.ephemeral_system_prompt
            return {"api_calls": 1, "final_response": "progress recorded"}

    def construct(agent_id, body, frames, session_key):
        seen["construct"] = (body, session_key)
        return Agent()

    monkeypatch.setattr(turn, "_construct_agent", construct)
    terminals = []
    real_terminal = turn._terminal_scope
    monkeypatch.setattr(turn, "_terminal_scope", lambda a, key: (terminals.append(key), real_terminal(a, key))[1])
    turn.register_paperclip_run(IDENTITY, REQUEST)
    outcome = turn._run_native_paperclip(IDENTITY, REQUEST, ROOM_KEY)
    assert outcome[:3] == ("completed", None, "progress recorded")
    assert seen["construct"] == ({"sessionKey": ROOM_KEY, "room": {"id": ROOM}}, ROOM_KEY)
    assert terminals == [ROOM_KEY]
    assert seen["run"]["system_message"] is None and seen["system"] == REQUEST["instructions"]
    assert seen["run"]["conversation_history"] == [{"role": "user", "content": "fix the bug"}]
    assert seen["run"]["user_message"].endswith(REQUEST["input"]) and "Nothing you write in this turn is posted to the room" in seen["run"]["user_message"]
