"""A task's work runs in the task's own Paperclip session, as stock Paperclip does (owner ruling
2026-10-18, reversing 2026-10-04's issue -> room conversation routing, which let a room turn that
never recorded a disposition get the task blocked: ISOA-860). A room turn files the task with a
From section; the task's own run reports done / needs-you back to that room."""
import json


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
