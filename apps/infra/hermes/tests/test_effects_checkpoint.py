"""bridge/effects.py (wave2-Q Task 5): the F1 callback for external, irreversible effects only.

Shell and file work never calls the backend; the shipped scripts and tools do, once per call; `needs_approval`
ends the Worker turn `waiting_approval`; a backend that cannot answer refuses the call (fail closed); the
dev-only fault hooks fire exactly where the plan says and never otherwise."""

import sys

import pytest

from bridge import effects
from tests import fake_lb_checkpoint
from bridge.effects import EffectCall, EffectsCallback, classify

TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
RUN = "0b5c6c39-6a43-5b7e-9d1a-3f0f2d7c1a11"
AGENT = "a9e8d7c6-b5a4-4392-8170-6f5e4d3c2b1a"
IDENTITY = {"runId": RUN, "ownerId": "org_test", "taskId": TASK, "attempt": 2, "agentId": AGENT, "runSecret": "s3cr3t"}
MAIL = "lightbulb-mail.sh send --to a@b.c --subject \"S\" <<'EOF'\nhi there\nEOF"


class Ref:
    def __init__(self, name, args, call_id="c1"):
        self.tool_call_id, self.name, self.args = call_id, name, args


# --- classify ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("terminal", {"command": "ls -la"}),
        ("terminal", {"command": "git commit -am 'x' && git push"}),
        ("terminal", {"command": "curl https://example.com"}),
        ("terminal", {"command": "lightbulb-mail.sh inbox --limit 5"}),
        ("terminal", {"command": "lightbulb app status --json"}),
        ("terminal", {"command": "gh pr view 7"}),
        ("write_file", {"path": "/tmp/x", "content": "y"}),
        ("web_search", {"query": "acme"}),
        ("lightbulb_network", {"action": "directory", "query": "sam"}),
        ("terminal", {}),
    ],
)
def test_shell_and_files_are_not_effects(tool, args):
    assert classify(tool, args, agent_id=AGENT) is None


def test_classify_mail_heredoc():
    call = classify("terminal", {"command": MAIL}, agent_id=AGENT)
    assert call == EffectCall(
        "mail.send", {"agentId": AGENT, "to": "a@b.c", "subject": "S", "body": "hi there"}, executor_ok=True
    )


def test_classify_mail_owner_reply_and_as():
    owner = classify("terminal", {"command": "lightbulb-mail.sh send --owner --subject S <<EOF\nx\nEOF"}, agent_id=AGENT)
    assert owner.operands == {"agentId": AGENT, "subject": "S", "body": "x"} and owner.executor_ok
    reply = classify(
        "terminal", {"command": "lightbulb-mail.sh send --as other --reply K1 <<'EOF'\nok\nEOF"}, agent_id=AGENT
    )
    assert reply.operands == {"agentId": "other", "replyKey": "K1", "body": "ok"} and reply.executor_ok


def test_classify_unparsed_mail_has_no_executor():
    piped = classify("terminal", {"command": "cat draft.txt | lightbulb-mail.sh send --to a@b.c --subject S"}, agent_id=AGENT)
    assert piped.kind == "mail.send" and not piped.executor_ok and "body" not in piped.operands
    assert piped.operands["to"] == "a@b.c"


def test_classify_text_send():
    call = classify("terminal", {"command": "/skills/lightbulb-text.sh send --reply K9 <<'EOF'\nOn my way\nEOF"}, agent_id=AGENT)
    assert call == EffectCall("text.send", {"agentId": AGENT, "replyKey": "K9", "body": "On my way"}, executor_ok=True)


def test_classify_app_publish():
    bare = classify("terminal", {"command": "lightbulb app publish --json"}, agent_id=AGENT)
    assert bare.kind == "app.publish" and not bare.executor_ok  # the folder names the app, not the command
    named = classify("terminal", {"command": "lightbulb app publish --app app_123 --json"}, agent_id=AGENT)
    assert named == EffectCall("app.publish", {"appId": "app_123"}, executor_ok=True)


def test_classify_gh_pr_merge():
    url = classify("terminal", {"command": "gh pr merge https://github.com/acme/web/pull/7 --squash"}, agent_id=AGENT)
    assert url == EffectCall("pr.merge", {"repo": "acme/web", "number": 7, "method": "squash"}, executor_ok=True)
    flag = classify("terminal", {"command": "gh pr merge 7 -R acme/web"}, agent_id=AGENT)
    assert flag.operands == {"repo": "acme/web", "number": 7, "method": "merge"} and flag.executor_ok
    bare = classify("terminal", {"command": "cd web && gh pr merge 7 --rebase"}, agent_id=AGENT)
    assert bare.kind == "pr.merge" and bare.operands == {"number": 7, "method": "rebase"} and not bare.executor_ok


def test_classify_tools():
    buy = classify("request_purchase", {"merchant": "Acme", "url": "https://acme.io", "amount": 35.0, "why": "hosting", "issue_id": "i1"}, agent_id=AGENT)
    assert buy == EffectCall("purchase", {"merchant": "Acme", "url": "https://acme.io", "amount": 35.0, "why": "hosting"}, executor_ok=False)
    send = classify("lightbulb_network", {"action": "send", "to": "sam-mac", "text": "hi"}, agent_id=AGENT)
    assert send == EffectCall("a2a.send", {"to": "sam-mac", "text": "hi"}, executor_ok=False)


def test_two_effects_in_one_command_are_multiple():
    call = classify("terminal", {"command": f"{MAIL}\n{MAIL}"}, agent_id=AGENT)
    assert call == EffectCall(effects.MULTIPLE, {"kinds": ["mail.send", "mail.send"]}, executor_ok=False)
    both = classify("terminal", {"command": "gh pr merge 7 -R a/b && lightbulb app publish --app x"}, agent_id=AGENT)
    assert both.kind == effects.MULTIPLE and both.operands == {"kinds": ["pr.merge", "app.publish"]}


@pytest.mark.parametrize(
    "command",
    [
        "env FOO=1 lightbulb-mail.sh send --to a@b.c --subject S <<'EOF'\nhi\nEOF",
        "FOO=1 BAR=2 lightbulb-mail.sh send --to a@b.c --subject S <<'EOF'\nhi\nEOF",
        "timeout 30 lightbulb-mail.sh send --to a@b.c --subject S <<'EOF'\nhi\nEOF",
        "timeout -s KILL 30 lightbulb-mail.sh send --to a@b.c --subject S <<'EOF'\nhi\nEOF",
        "nohup lightbulb-mail.sh send --to a@b.c --subject S <<'EOF'\nhi\nEOF",
        "sudo -u agent lightbulb-mail.sh send --to a@b.c --subject S <<'EOF'\nhi\nEOF",
        "bash -c 'lightbulb-mail.sh send --to a@b.c --subject S'",
        "sh -c \"cd /w && lightbulb-mail.sh send --to a@b.c --subject S\"",
    ],
)
def test_wrappers_and_sh_c_are_seen_through(command):
    call = classify("terminal", {"command": command}, agent_id=AGENT)
    assert call is not None and call.kind == "mail.send" and call.operands["to"] == "a@b.c"


def test_sh_c_with_two_actions_is_multiple():
    call = classify("terminal", {"command": "bash -c 'gh pr merge 7 -R a/b; lightbulb app publish --app x'"}, agent_id=AGENT)
    assert call.kind == effects.MULTIPLE


def test_operands_hash_is_stable_and_specific():
    a = effects.operands_hash(classify("terminal", {"command": MAIL}, agent_id=AGENT))
    b = effects.operands_hash(classify("terminal", {"command": MAIL}, agent_id=AGENT))
    c = effects.operands_hash(classify("terminal", {"command": MAIL.replace("hi there", "bye")}, agent_id=AGENT))
    assert a == b != c and len(a) == 64


# --- callback -------------------------------------------------------------------------------------------


@pytest.fixture
def backend(monkeypatch):
    """The effects route, faked: `answers` is what POSTs return (status, body); `calls` records them."""
    monkeypatch.setitem(sys.modules, "agent.lb_checkpoint", fake_lb_checkpoint)
    state = {"answers": [(200, {"decision": "proceed"})], "calls": []}

    def post(path, body, *, timeout_s=8.0):
        state["calls"].append((path, body))
        answer = state["answers"].pop(0) if len(state["answers"]) > 1 else state["answers"][0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(effects, "_post", post)
    exits = []
    monkeypatch.setattr(effects, "_exit", lambda code: exits.append(code))
    state["exits"] = exits
    return state


def _cb(faults=()):
    return EffectsCallback(IDENTITY, {"input": "x", "instructions": "y", "faults": list(faults)})


def test_plain_tools_never_call_the_backend(backend):
    cb = _cb()
    assert cb.before_tool(Ref("terminal", {"command": "ls"})).kind == "proceed"
    cb.tool_committed(Ref("terminal", {"command": "ls"}), "ok", "done")
    assert backend["calls"] == [] and cb.outcome is None


def test_proceed_posts_once_and_commits_done(backend):
    backend["answers"] = [(200, {"decision": "proceed"})]
    cb = _cb()
    ref = Ref("terminal", {"command": MAIL}, call_id="call-9")
    assert cb.before_tool(ref).kind == "proceed"
    cb.tool_committed(ref, '{"status":"sent"}', "done")
    [(path, body), (done_path, done_body)] = backend["calls"]
    assert path == "/api/v1/internal/runtime/org_test/effects"
    assert body["idemKey"] == f"{TASK}:2:call-9" and body["taskId"] == TASK and body["attempt"] == 2
    assert body["runId"] == RUN and body["runSecret"] == "s3cr3t" and body["toolCallId"] == "call-9"
    assert body["kind"] == "mail.send" and body["tool"] == "terminal" and body["executorOk"] is True
    assert body["operands"]["body"] == "hi there" and body["operandsHash"] == effects.operands_hash(classify("terminal", {"command": MAIL}, agent_id=AGENT))
    assert done_path == f"/api/v1/internal/runtime/org_test/effects/{TASK}:2:call-9/done"
    assert done_body == {"disposition": "done", "resultRef": None, "runSecret": "s3cr3t", "runId": RUN}
    assert cb.outcome is None


def test_needs_approval_ends_turn_waiting_approval(backend):
    backend["answers"] = [(200, {"decision": "needs_approval", "questionId": "q-abc"})]
    cb = _cb()
    ref = Ref("terminal", {"command": MAIL})
    decision = cb.before_tool(ref)
    assert decision.kind == "refuse" and "q-abc" in decision.message and "End your turn" in decision.message
    assert cb.outcome == "waiting_approval"
    cb.tool_committed(ref, "blocked", None)  # never ran: nothing to commit
    assert len(backend["calls"]) == 1


def test_denied_refuses_without_waiting(backend):
    backend["answers"] = [(200, {"decision": "denied", "questionId": "q-d"})]
    cb = _cb()
    decision = cb.before_tool(Ref("terminal", {"command": MAIL}))
    assert decision.kind == "refuse" and "q-d" in decision.message and "not" in decision.message.lower()
    assert cb.outcome is None  # the person already answered; the Worker carries on with other work


def test_replay_returns_the_stored_result(backend):
    backend["answers"] = [(200, {"decision": "replay", "result": "sent earlier"})]
    decision = _cb().before_tool(Ref("terminal", {"command": MAIL}))
    assert decision.kind == "replay" and decision.result == "sent earlier"


def test_refuse_passes_the_message(backend):
    backend["answers"] = [(200, {"decision": "refuse", "message": "A matching call (attempt 1) has an unknown outcome; check before retrying."})]
    decision = _cb().before_tool(Ref("terminal", {"command": MAIL}))
    assert decision.kind == "refuse" and "attempt 1" in decision.message


@pytest.mark.parametrize("answer", [(503, {"error": "temporal_unavailable"}), (404, {}), OSError("timeout"), (200, {"decision": "nope"})])
def test_backend_unavailable_fails_closed(backend, answer):
    backend["answers"] = [answer]
    decision = _cb().before_tool(Ref("terminal", {"command": MAIL}))
    assert decision.kind == "refuse" and "Do not retry" in decision.message


def test_commit_failure_is_logged_not_raised(backend):
    backend["answers"] = [(200, {"decision": "proceed"}), OSError("down")]
    cb = _cb()
    ref = Ref("terminal", {"command": MAIL})
    cb.before_tool(ref)
    cb.tool_committed(ref, "sent", "done")  # the backend's intent row now reads as unknown; the gate refuses a repeat


def test_disposition_mapping(backend):
    cb = _cb()
    ref = Ref("terminal", {"command": MAIL})
    cb.before_tool(ref)
    cb.tool_committed(ref, "sent", None)
    assert backend["calls"][-1][1]["disposition"] == "done"  # ran and committed without a tool error
    cb.before_tool(Ref("terminal", {"command": MAIL}, "c2"))
    cb.tool_committed(Ref("terminal", {"command": MAIL}, "c2"), '{"error":"HTTP 500"}', None)
    assert backend["calls"][-1][1]["disposition"] == "unknown"


def test_kill_after_effect_intent_hook(backend):
    cb = _cb(["kill_after_effect_intent"])
    cb.before_tool(Ref("terminal", {"command": MAIL}))
    assert backend["exits"] == [70] and len(backend["calls"]) == 1  # died right after the proceed, before the tool


def test_kill_after_tool_before_done_hook(backend):
    cb = _cb(["kill_after_tool_before_done"])
    ref = Ref("terminal", {"command": MAIL})
    cb.before_tool(ref)
    assert backend["exits"] == []
    cb.tool_committed(ref, "sent", "done")
    assert backend["exits"] == [70] and len(backend["calls"]) == 1  # died before the done post


def test_no_faults_no_exit(backend):
    cb = _cb()
    ref = Ref("terminal", {"command": MAIL})
    cb.before_tool(ref)
    cb.tool_committed(ref, "sent", "done")
    assert backend["exits"] == []


def test_two_effects_in_one_command_refused_without_backend(backend):
    cb = _cb()
    decision = cb.before_tool(Ref("terminal", {"command": f"{MAIL}\n{MAIL}"}))
    assert decision.kind == "refuse" and "split" in decision.message.lower()
    assert backend["calls"] == [] and cb.outcome is None


def test_replay_of_a_refusal_is_refused(backend):
    backend["answers"] = [(200, {"decision": "replay", "result": "refused:rate_limited"})]
    decision = _cb().before_tool(Ref("terminal", {"command": MAIL}))
    assert decision.kind == "refuse" and "rate_limited" in decision.message


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("sent\n[exit code 0]", "done"),
        ("HTTP 500: boom\n[exit code 1]", "unknown"),
        ("Command exited with code 2", "unknown"),
        ('{"stdout": "", "exit_code": 1}', "unknown"),
        ('{"stdout": "sent", "exit_code": 0}', "done"),
        ("sent", "done"),
    ],
)
def test_plain_text_exit_codes_decide_the_disposition(backend, result, expected):
    cb = _cb()
    ref = Ref("terminal", {"command": MAIL})
    cb.before_tool(ref)
    cb.tool_committed(ref, result, None)
    assert backend["calls"][-1][1]["disposition"] == expected
