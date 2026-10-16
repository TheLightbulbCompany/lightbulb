import json
from bridge import turn
from tests import factories as f

ROOT = "55555555-5555-4555-8555-555555555555"


def test_context_carries_the_message_id_and_thread_root():
    body = f.ingress(message=f.message(threadRootMessageId=ROOT))
    ctx = turn.room_context(body, "ceo")
    assert ctx["message"]["id"] == body["message"]["messageId"]
    assert ctx["message"]["threadRootMessageId"] == ROOT
    assert ctx["eventKind"] == "user_request"


def test_context_carries_sessions_participants_and_mentions_verbatim():
    sessions = [{"sessionId": f.ROOM, "humanUserId": "user_1", "displayName": "Prez",
                 "provider": "claude", "cwdName": "lightbulb", "title": None,
                 "status": "idle", "latestEventSeq": 4, "lastEventAt": None}]
    ctx = turn.room_context(f.ingress(sessions=sessions), "ceo")
    assert ctx["sessions"] == sessions
    assert {p["id"] for p in ctx["participants"]} == {"user_1", "ceo"}
    assert ctx["mentions"] == []


def test_the_system_prompt_embeds_the_context_as_parseable_json():
    prompt = turn.system_prompt(f.ingress(), "ceo")
    block = prompt.split("```json", 1)[1].split("```", 1)[0]
    assert json.loads(block)["room"]["name"] == "general"
    assert "CEO" in prompt


# ---- review fix (cheap f): the user_message prefix uses the sender's displayName, like
# _history does for every other participant, instead of their bare id.
def test_the_turn_user_message_prefixes_with_the_senders_display_name():
    body = f.ingress()  # sender is {"kind": "human", "id": "user_1", "displayName": "Prez"}
    assert turn._turn_user_message(body) == "Prez: ship it"


def test_the_turn_user_message_falls_back_to_id_with_no_display_name():
    body = f.ingress(message=f.message(author={"kind": "human", "id": "user_9"}))
    assert turn._turn_user_message(body) == "user_9: ship it"


def test_the_turn_user_message_names_referenced_sessions():
    body = {**f.ingress(), "referencedSessions": [
        {"sessionId": "s1", "provider": "codex", "title": "Launch check", "status": "idle"}]}
    assert turn._turn_user_message(body) == "Prez: ship it\nReferenced sessions: Codex · Launch check (idle) [s1]"


def test__run_hermes_passes_the_display_name_prefixed_user_message(home, monkeypatch):
    seen = {}

    class _FakeAgent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **kw):
            seen["user_message"] = kw["user_message"]
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _FakeAgent())
    turn._run_hermes("ceo", f.ingress(), None)
    _, text = f.split_turn_input(seen["user_message"])
    assert text == "Prez: ship it"


# ---- prompt-cache stability: providers cache a byte-identical PREFIX, and the room context
# is spliced into the top of the system prompt, so anything per-turn there made every turn a
# cache miss (0 of ~16k tokens cached, measured on dev 2026-09-23).
def test_the_system_prompt_is_byte_identical_across_turns_in_one_room():
    a = f.ingress(message=f.message(text="first"), sessions=[{"sessionId": "s1"}])
    b = f.ingress(message=f.message(text="second", threadRootMessageId=ROOT),
                  participants=list(reversed(a["participants"])), eventKind="room_event")
    b["mentions"] = [{"kind": "agent", "id": "ceo"}]
    assert turn.system_prompt(a, "ceo", media=["/mnt/x.png"]) == turn.system_prompt(b, "ceo")


def test_per_turn_fields_move_to_the_user_message():
    body = f.ingress(message=f.message(threadRootMessageId=ROOT), sessions=[{"sessionId": "s1"}])
    prompt = turn.system_prompt(body, "ceo")
    header, _, rest = turn._turn_input(body, media=["/mnt/x.png"]).partition("\n")
    ctx = json.loads(header.removeprefix(turn.TURN_CONTEXT_PREFIX))
    assert body["message"]["messageId"] not in prompt and "s1" not in prompt
    assert ctx["message"]["id"] == body["message"]["messageId"]
    assert ctx["message"]["threadRootMessageId"] == ROOT
    assert ctx["sessions"] == [{"sessionId": "s1"}] and ctx["media"] == ["/mnt/x.png"]
    assert rest == "Prez: ship it"


def test_typed_room_turns_and_paperclip_runs_write_chat_like_a_person_but_mail_and_calls_do_not():
    body = f.ingress()
    assert turn.CHAT_STYLE in turn.system_prompt(body, "ceo")
    assert turn.CHAT_STYLE in turn.system_prompt({**body, "eventKind": "paperclip_run"}, "ceo")
    assert turn.CHAT_STYLE not in turn.system_prompt({**body, "eventKind": "agent_mail"}, "ceo")
    assert turn.CHAT_STYLE not in turn.system_prompt(body, "ceo", spoken=True)
