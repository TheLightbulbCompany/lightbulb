"""Spark autocomplete (/os/assist/complete and /os/assist/reach), over moto DynamoDB with real Clerk session tokens and
a fake provider: no test calls a real model."""

import gzip
import json
import logging
import math
from dataclasses import replace

import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from core import dynamodb
from core.config import settings
from core.services import autocomplete as ac
from main import app
from tests.unit.auth.conftest import Tenant, jwks  # noqa: F401
from tests.unit.routers.test_os import DAY, START, clock, users  # noqa: F401

A = Tenant("user_a", None)
SECRET = "Zanzibar-7731 quarterly"  # a string that must never reach a log line


class Fake:
    name = "openai"

    def __init__(self, text="#80 thread\n Sure, Tuesday works. See you then.", logprobs=None):
        self.text, self.logprobs, self.calls = text, logprobs, []

    def configured(self):
        return True

    async def complete(self, developer, user, max_tokens, cache_key, timeout_s):
        parts = [user] if isinstance(user, str) else user
        self.calls.append(
            {
                "developer": developer,
                "user": "".join(parts),
                "parts": parts,
                "max_tokens": max_tokens,
                "cache_key": cache_key,
            }
        )
        return ac.Completion(self.text, self.logprobs, 1200, 1000, 12)


@pytest.fixture
def on(monkeypatch, users, clock):  # noqa: F811
    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_ENABLED", True)
    fake = Fake()
    monkeypatch.setattr(ac, "provider", fake)
    return fake


BODY = {
    "before": "Thanks for the note. ",
    "screen": f"Sam: can we meet Tuesday about {SECRET}?\n\n⟦field⟧",
    "conversation": {"kind": "chat", "header": "Sam", "recipients": ["Sam"]},
    "last_is_mine": False,
    "bar": -1.0,
}


async def post(path="/os/assist/complete", body=None, who: Tenant | None = A, headers=None, method="POST", raw=None):
    h = (who.headers if who else {}) | (headers or {})
    content = raw if raw is not None else json.dumps(BODY if body is None else body).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(
            method, f"/api/v1{path}", headers=h | {"content-type": "application/json"}, content=content
        )


PATHS = ["/os/assist/complete", "/os/assist/reach"]


# --- the environment switch ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("method", ["POST", "GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
async def test_off_answers_as_if_the_route_did_not_exist(users, clock, monkeypatch, path, method):  # noqa: F811
    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_ENABLED", False)
    fake = Fake()
    monkeypatch.setattr(ac, "provider", fake)
    unknown = await post("/os/assist/nothing-here", method=method)
    for who in (A, None):  # signed in or not: no 401 that would show the route is there
        r = await post(path, who=who, method=method)
        assert r.status_code == unknown.status_code == 404 and r.content == unknown.content
    assert fake.calls == [] and users.scan()["Items"] == []


def test_the_switch_is_off_by_default():
    assert type(settings).model_fields["SPARK_AUTOCOMPLETE_ENABLED"].default is False


# --- auth and entitlement -----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("token", ["oauth", "template"])
async def test_only_a_clerk_session_may_call(on, users, path, token):  # noqa: F811
    assert (await post(path, who=replace(A, token=token))).status_code == 403
    assert (await post(path, who=None)).status_code in (401, 403)
    assert on.calls == [] and users.scan()["Items"] == []


@pytest.mark.parametrize("path", PATHS)
async def test_using_it_starts_the_trial_and_an_ended_trial_is_402(on, clock, path):  # noqa: F811
    """An account that never talks is not in trial forever: the first call starts the clock, as a first conversation
    does, and seven days later the answer is 402 with no model call."""
    assert (await post(path)).status_code == 200
    row = dynamodb.get_table("users").get_item(Key={"user_id": "user_a"})["Item"]
    assert row["os_trial_started_at"] == START and row["os_trial_ends_at"] == START + 7 * DAY
    clock[0] += 7 * DAY
    on.calls.clear()
    r = await post(path)
    assert (r.status_code, r.json()["detail"]) == (402, "trial_ended")
    assert on.calls == []


@pytest.mark.parametrize("path", PATHS)
async def test_a_paying_or_team_account_is_served_after_the_trial(on, clock, monkeypatch, path):  # noqa: F811
    monkeypatch.setattr(settings, "SPARK_INTERNAL_USER_IDS", "user_a")
    await post(path)
    clock[0] += 30 * DAY
    assert (await post(path)).status_code == 200


async def test_no_provider_key_is_503(on, monkeypatch):
    monkeypatch.setattr(on, "configured", lambda: False)
    r = await post()
    assert (r.status_code, r.json()["detail"]) == (503, "autocomplete_unavailable")


# --- the body -----------------------------------------------------------------------------------------------


async def test_gzip_is_accepted(on):
    r = await post(raw=gzip.compress(json.dumps(BODY).encode()), headers={"content-encoding": "gzip"})
    assert r.status_code == 200 and r.json()["show"] is True


@pytest.mark.parametrize(
    "raw,enc,code",
    [
        (b"x" * 10, "br", 415),
        (b"not gzip", "gzip", 400),
        (gzip.compress(b"{" * 10, mtime=0)[:-6], "gzip", 400),  # cut short
        (b"[1, 2]", None, 400),
        (b"not json", None, 400),
        (json.dumps({"before": 5}).encode(), None, 400),  # a known key of the wrong type
        (json.dumps({"clipboard": "text"}).encode(), None, 400),
    ],
)
async def test_bad_bodies_are_refused(on, raw, enc, code):
    r = await post(raw=raw, headers={"content-encoding": enc} if enc else None)
    assert r.status_code == code
    assert on.calls == []


async def test_over_the_byte_limit_is_413_sent_or_unzipped(on, monkeypatch):
    from routers import os_autocomplete

    monkeypatch.setattr(os_autocomplete, "BODY_MAX_BYTES", 1000)
    big = json.dumps({"before": "a" * 2000}).encode()
    assert (await post(raw=big)).status_code == 413
    bomb = gzip.compress(big)
    assert len(bomb) < 1000
    assert (await post(raw=bomb, headers={"content-encoding": "gzip"})).status_code == 413
    assert on.calls == []


async def test_fields_over_their_caps_are_cut_and_unknown_keys_ignored(on):
    body = BODY | {
        "before": "x" * 30_500 + " end of draft. ",
        "after": "y" * 12_000,
        "screen": "s" * 70_000 + "⟦field⟧" + "t" * 70_000,
        "window_title": "w" * 400,
        "declined": "😀" * 200,  # 400 UTF-16 units
        "voice": [{"text": "v" * 500, "app": "Mail"}] * 5,
        "mind": [{"text": "m"}] * 50,
        "conversation": {"header": "h" * 200, "recipients": [f"r{i}" for i in range(20)]},
        "clipboard": {"text": "c" * 2000, "ago_s": 10, "from": "Notes"},
        "brand_new_key": {"anything": True},
    }
    assert (await post(body=body)).status_code == 200
    req = ac.CompleteRequest.model_validate(body)
    assert req.before.endswith("end of draft. ") and len(req.before) == 30_000
    assert req.after == "y" * 10_000
    assert len(req.screen) == 60_000 and req.screen.index("⟦field⟧") == 40_000
    assert len(req.window_title) == 300
    assert ac.utf16_len(req.declined) == 300 and req.declined == "😀" * 150
    assert len(req.voice) == 3 and req.voice[0].text == "v" * 400
    assert len(req.mind) == 40
    assert len(req.conversation.header) == 160 and len(req.conversation.recipients) == 12
    assert len(req.clipboard.text) == 1000
    assert ac.CompleteRequest.model_validate({"clipboard": {"text": "old", "ago_s": 181}}).clipboard is None
    assert ac.CompleteRequest.model_validate({"bar": -9}).bar == -2.0
    assert ac.CompleteRequest.model_validate({"bar": 3}).bar == -0.2


def test_memory_and_flow_share_their_pools():
    req = ac.CompleteRequest.model_validate(
        {
            "memory": [{"text": "a" * 16_000}] * 10,
            "recent_flow": [{"text": "b" * 16_000, "structured": "c" * 16_000}] * 4,
        }
    )
    assert sum(len(m.text) for m in req.memory) == ac.MEMORY_TOTAL
    assert sum(len(f.text) for f in req.recent_flow) == ac.FLOW_POOL
    assert sum(len(f.structured) for f in req.recent_flow) == ac.FLOW_POOL


# --- the daily ceiling --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_the_daily_ceiling_is_429_and_resets_the_next_day(on, clock, monkeypatch, path):  # noqa: F811
    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_DAILY_MAX", 3)
    for _ in range(3):
        assert (await post(path)).status_code == 200
    r = await post(path)
    assert (r.status_code, r.json()["detail"]) == (429, "daily_limit")
    assert len(on.calls) == 3
    clock[0] += DAY
    assert (await post(path)).status_code == 200


async def test_both_routes_share_one_counter(on, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_DAILY_MAX", 2)
    assert (await post("/os/assist/complete")).status_code == 200
    assert (await post("/os/assist/reach")).status_code == 200
    assert (await post("/os/assist/complete")).status_code == 429
    assert (await post("/os/assist/reach")).status_code == 429


async def test_the_daily_character_budget_is_429(on, monkeypatch):
    on_first = await post()
    assert on_first.status_code == 200
    used = len(on.calls[0]["developer"]) + len(on.calls[0]["user"])
    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_DAILY_CHARS", used * 2 + 10)
    assert (await post()).status_code == 200
    assert (await post()).json()["detail"] == "daily_limit"  # a third prompt would pass the budget
    assert len(on.calls) == 2
    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_DAILY_CHARS", used - 1)
    assert (await post()).status_code == 429  # one prompt over the whole budget is never sent


async def test_a_request_from_yesterday_never_resets_today(on, clock, monkeypatch):  # noqa: F811
    from routers import os_autocomplete

    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_DAILY_MAX", 2)
    await post()
    clock[0] += DAY
    assert (await post()).status_code == 200 and (await post()).status_code == 200
    assert await os_autocomplete._within_daily_max("user_a", clock[0] - DAY, 10) is False  # late, stale day
    assert (await post()).status_code == 429


async def test_a_lone_surrogate_is_400_before_anything_is_counted(on):
    r = await post(raw=b'{"before": "hi \\ud800"}')
    assert (r.status_code, r.json()["detail"]) == (400, "invalid_request")
    assert on.calls == []


async def test_free_quiet_answers_are_not_counted(on, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_DAILY_MAX", 1)
    for _ in range(3):
        assert (await post(body={"before": "   "})).json() == {"show": False, "reason": "no_draft", "confidence": None}
    assert on.calls == []
    assert (await post()).status_code == 200


# --- the suggestion -----------------------------------------------------------------------------------------


async def test_a_suggestion_has_the_opsis_reply_shape(on):
    a = (await post()).json()
    assert a["show"] is True and a["kind"] == "cont"
    assert a["insert"] == " Sure, Tuesday works. See you then."
    assert a["option"][0] == a["insert"]
    assert a["source"] == {"kind": "thread"} and a["option_sources"] == [{"kind": "thread"}]
    assert a["locked_words"] == 6 and a["provider"] == "openai" and a["id"].startswith("ac_")
    assert on.calls[0]["max_tokens"] == 80


async def test_compose_writes_three_versions_and_option_0_is_insert(on):
    on.text = "#70 thread\nYes, Tuesday at 3 works.\n#50 thread\nNot this week, sorry.\n#30 screen\nWhich Tuesday?"
    a = (await post(body=BODY | {"trigger": "summon", "before": ""})).json()
    assert a["kind"] == "reply" and a["insert"] == "Yes, Tuesday at 3 works."
    assert a["option"] == ["Yes, Tuesday at 3 works.", "Not this week, sorry.", "Which Tuesday?"]
    assert a["option"][0] == a["insert"]
    assert a["option_sources"] == [{"kind": "thread"}, {"kind": "thread"}, {"kind": "screen"}]
    assert "Write exactly 3 versions" in on.calls[0]["developer"]


async def test_another_asks_for_three_grey_versions(on):
    on.text = "#60 message\n soon.\n#40 message\n tomorrow."
    a = (await post(body=BODY | {"trigger": "another"})).json()
    assert a["option"] == [" soon.", " tomorrow."] and a["option"][0] == a["insert"]
    assert "Write exactly 3 version(s)" in on.calls[0]["developer"]


async def test_the_declined_suggestion_is_never_returned_and_is_in_the_prompt(on):
    on.text = "#90 message\n see you Tuesday.\n#50 message\n talk soon."
    a = (await post(body=BODY | {"trigger": "another", "declined": "See you  tuesday."})).json()
    assert a["insert"] == " talk soon." and a["option"] == [" talk soon."]
    assert "<declined>\nSee you  tuesday.\n</declined>" in on.calls[0]["user"]


@pytest.mark.parametrize(
    "text,reason",
    [
        ("#0", "model_quiet"),
        ("", "model_quiet"),
        ("#0 message\n anything", "model_quiet"),
        ("#90 message\n" + "line\n" * 14, "model_quiet"),  # more than 12 newlines: the Mac would drop it
        ("#90 message\n a\n\n\nb", "model_quiet"),
        ("#90 message\n   ", "model_quiet"),
    ],
)
async def test_the_model_staying_quiet_is_show_false(on, text, reason):
    on.text = text
    a = (await post()).json()
    assert a["show"] is False and a["reason"] == reason and "insert" not in a


async def test_below_the_bar_is_quiet_by_token_logprobs(on):
    on.text = "#95 message\n Sure, fine."
    head = len("#95 message\n".encode())
    # the header's tokens are sure; the suggestion's tokens average −1.5
    on.logprobs = [(head, -0.01), (len(b" Sure"), -1.0), (len(b","), -2.0), (len(b" fine."), -1.5)]
    assert (await post(body=BODY | {"bar": -1.0})).json() == {"show": False, "reason": "below_bar", "confidence": -1.5}
    shown = (await post(body=BODY | {"bar": -2.0})).json()
    assert shown["show"] is True and shown["confidence"] == -1.5


async def test_without_logprobs_the_bar_reads_the_models_own_estimate(on):
    on.text = "#30 message\n Sure."  # ln(0.30) = −1.204
    assert (await post(body=BODY | {"bar": -1.0})).json()["reason"] == "below_bar"
    assert (await post(body=BODY | {"bar": -1.5})).json()["show"] is True


async def test_tab_tab_ignores_the_bar(on):
    on.text = "#5 thread\nMaybe."
    assert (await post(body=BODY | {"trigger": "summon", "bar": -0.2})).json()["show"] is True


def test_score_reads_bar_as_mean_token_logprob():
    out = "#80 thread\n Yes, sure."
    v = ac.versions(out)[0]
    comp = ac.Completion(out, [(len(b"#80 thread\n"), 0.0), (len(b" Yes"), -0.5), (len(b", sure."), -1.5)])
    assert ac.score(out, comp, v) == -1.0
    assert math.isclose(ac.score(out, ac.Completion(out, None), v), math.log(0.8))


@pytest.mark.parametrize(
    "text,parts",
    [
        ("Sure, I can do Tuesday. Does 3pm work?", ["Sure, ", "I can do Tuesday. ", "Does 3pm work?"]),
        (" sounds good", [" sounds good"]),
        ("It costs 3.50 today; ok", ["It costs 3.50 today; ", "ok"]),
        ('He said "fine." Then left', ['He said "fine." ', "Then left"]),
        ("Hi Sam,\nYes.\nBest", ["Hi Sam,\n", "Yes.\n", "Best"]),
        ("😀 wow, yes", ["😀 wow, ", "yes"]),
    ],
)
def test_chunks_break_after_each_clause_in_utf16_units(text, parts):
    assert "".join(parts) == text
    assert ac.chunks(text) == [ac.utf16_len(p) for p in parts]


async def test_the_reply_chunks_match_the_insert(on):
    a = (await post()).json()
    assert a["chunks"] == ac.chunks(a["insert"]) == [7, 15, 13]  # " Sure, " "Tuesday works. " "See you then."


async def test_the_prompt_uses_every_context_field_stable_parts_first(on):
    body = BODY | {
        "window_title": "Chat — Sam",
        "field": {"label": "Message"},
        "style": ["STYLE-1 see you there", "ok sounds good"],
        "voice": [{"text": "VOICE-1", "app": "Slack", "ago_s": 30}],
        "mind": [{"text": "MIND-1", "day": "today"}],
        "recent_work": ["WORK-1"],
        "memory": [{"text": "MEMORY-1", "title": "Notes"}],
        "reaches_for": ["REACH-1"],
        "recent_flow": [
            {
                "title": "Calendar",
                "text": "FLOW-1",
                "ago_s": 40,
                "stay_s": 12,
                "now": False,
                "view": "1f-2",
                "pane": "a",
            },
            {"title": "Chat", "text": "CURRENT-VISIT", "now": True},
        ],
        "eager": 0.5,
        "clipboard": {"text": "CLIP-1", "ago_s": 5, "from": "Safari"},
        "declined": "DECLINED-1",
        "after": "AFTER-1",
        "utc_offset_min": 60,
    }
    await post(body=body)
    user = on.calls[0]["user"]
    order = [
        "VOICE-1",
        "STYLE-1",
        "MIND-1",
        "WORK-1",
        "FLOW-1",
        "writing to: Sam",
        "field label: Message",
        "⟦field⟧",
        "MEMORY-1",
        "REACH-1",
        "CLIP-1",
        "<last_is_mine>\nfalse",
        "DECLINED-1",
        "<ages>\nV1: written 30 s ago\nW1: visited 40 s ago\n</ages>",
        "<local_time>",
        "Thanks for the note. ⟦cursor⟧AFTER-1",
    ]
    at = [user.index(x) for x in order]
    assert at == sorted(at)
    assert user.endswith("</draft>")
    assert "CURRENT-VISIT" not in user  # the current visit is <screen> already
    assert "[V1, Slack]\nVOICE-1" in user and "[W1 · Calendar, stayed 12 s]\nFLOW-1" in user
    parts = on.calls[0]["parts"]
    assert len(parts) == 5 and parts[-1].startswith("<reaches_for>")  # person, windows, this window, memory, now
    assert on.calls[0]["cache_key"] == ac.cache_key("user_a") and "user_a" not in on.calls[0]["cache_key"]


def _pause(i: int) -> dict:
    """The same place, i pauses later: the draft has grown and every relative age has ticked."""
    return BODY | {
        "before": "Thanks for the note, " + "and more " * i,
        "voice": [{"text": "VOICE", "app": "Slack", "ago_s": 30 + 3 * i}],
        "style": ["see you there soon"],
        "mind": [{"text": "MIND", "day": "today"}],
        "recent_flow": [
            {
                "title": "Calendar",
                "text": "FLOW",
                "ago_s": 40 + 3 * i,
                "stay_s": 12,
                "earlier": [{"text": "E", "ago_s": 90 + 3 * i}],
            }
        ],
        "memory": [{"text": "MEMORY", "title": "Notes"}],
        "clipboard": {"text": "CLIP", "ago_s": 5 + 3 * i},
        "utc_offset_min": 60,
    }


async def test_only_the_last_part_changes_between_pauses_in_one_place(on):
    """A pause whose only changes are the draft and the clock gives the same text up to the last breakpoint."""
    for i in range(5):
        await post(body=_pause(i))
    cached = {tuple(c["parts"][:-1]) for c in on.calls}
    assert len(cached) == 1 and len(on.calls[0]["parts"]) == 5
    assert len({c["parts"][-1] for c in on.calls}) == 5
    for c in on.calls:
        assert " ago" not in "".join(c["parts"][:-1])  # nothing that ticks before a breakpoint


async def test_a_memory_change_keeps_the_person_window_and_screen_parts(on):
    await post(body=_pause(0))
    await post(body=_pause(1) | {"memory": [{"text": "OTHER MEMORY"}]})
    a, b = (c["parts"] for c in on.calls)
    assert a[:3] == b[:3] and a[3] != b[3]


async def test_the_parts_join_to_text_with_blank_lines_between(on):
    await post(body=_pause(0))
    parts = on.calls[0]["parts"]
    assert all(p.endswith(">\n\n") for p in parts[:-1])
    assert "\n\n\n" not in "".join(parts)


async def test_openai_gets_explicit_breakpoints_on_every_part_but_the_last(monkeypatch):
    ok = {"choices": [{"message": {"content": "#0"}}], "usage": {}}
    p, sent = _openai(monkeypatch, httpx.Response(200, json=ok), httpx.Response(200, json=ok))
    await p.complete("dev", ["A\n\n", "B\n\n", "C"], 80, "k", 4.0)
    body = sent[0]
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    assert body["messages"][1]["content"] == [
        {"type": "text", "text": "A\n\n", "prompt_cache_breakpoint": {"mode": "explicit"}},
        {"type": "text", "text": "B\n\n", "prompt_cache_breakpoint": {"mode": "explicit"}},
        {"type": "text", "text": "C"},
    ]
    await p.complete("dev", "reach prompt", 48, "k", 3.0)  # reach: one plain string, the default (implicit) mode
    assert sent[1]["messages"][1]["content"] == "reach prompt" and "prompt_cache_options" not in sent[1]


async def test_the_labelled_tree_replaces_the_plain_window_text_and_the_form_stays(on):
    tree = "‹list · Messages›\n[Them:]\nAre we still on?\n‹/›\n⟦field⟧"
    plain = "PLAIN-WINDOW Are we still on?\n⟦field⟧\n\nTo: Sam\nSubject: Friday"
    await post(body=BODY | {"screen": plain, "screen_structured": tree})
    user = on.calls[0]["user"]
    assert "[Them:]" in user and "PLAIN-WINDOW" not in user
    assert "<form>\nTo: Sam\nSubject: Friday\n</form>" in user
    await post(body=BODY | {"screen": plain, "screen_structured": None})
    user = on.calls[1]["user"]
    assert "<screen>\nPLAIN-WINDOW Are we still on?\n⟦field⟧\n</screen>" in user and "<form>\nTo: Sam" in user


@pytest.mark.parametrize(
    "screen,form",
    [
        ("window ⟦field⟧\n\nTo: Sam\nSubject: Hi there", "To: Sam\nSubject: Hi there"),
        ("window ⟦field⟧\n\nTo: Sam\nSubject: Hi there\njust a line", ""),  # not every line is a field
        ("window\n\nNote: ⟦field⟧", ""),  # the field itself is never the form
        ("window ⟦field⟧", ""),
    ],
)
def test_form_lines_are_the_trailing_block(screen, form):
    assert ac.split_form(screen)[1] == form


def test_the_914_shapes_are_accepted():
    req = ac.CompleteRequest.model_validate(
        {
            "style": [f"message {i} " + "x" * 500 for i in range(10)],
            "eager": 0.75,
            "conversation": {"kind": "dm", "header": "Sam", "recipients": []},
            "last_is_mine": None,
            "recent_work": [],
            "recent_flow": [{"app_id": "a", "view": "1f-2", "pane": "3a", "now": True, "earlier": []}],
        }
    )
    assert len(req.style) == 8 and all(ac.utf16_len(t) == 400 for t in req.style)
    assert req.eager == 0.75 and req.recent_flow[0].now is True
    assert ac.CompleteRequest.model_validate({"eager": 7}).eager == 1.0
    with pytest.raises(ValueError):
        ac.CompleteRequest.model_validate({"style": "chat"})  # a list now, not a hint
    with pytest.raises(ValueError):
        ac.CompleteRequest.model_validate({"eager": "high"})


@pytest.mark.parametrize("eager,shown", [(1.0, True), (0.5, False), (None, False), (0.0, False)])
async def test_eager_leans_the_bar_by_at_most_a_tenth(on, eager, shown):
    on.text = "#36 message\n Sure."  # ln(0.36) = −1.02: just under bar −1.0, over −1.1
    body = BODY | {"bar": -1.0} | ({"eager": eager} if eager is not None else {})
    assert (await post(body=body)).json()["show"] is shown


async def test_a_provider_failure_is_502(on, monkeypatch):
    async def fail(*a, **k):
        raise ac.ProviderError("timeout")

    monkeypatch.setattr(on, "complete", fail)
    r = await post()
    assert (r.status_code, r.json()["detail"]) == (502, "upstream_failed")


# --- reach --------------------------------------------------------------------------------------------------

REACH = {
    "app_id": "com.apple.mail",
    "window_title": "Re: trip",
    "field_label": "Body",
    "recipient": "Sam",
    "near": f"When does your flight land? {SECRET}",
    "before": "It lands at ",
    "after": "",
}


async def test_reach_returns_up_to_three_queries(on):
    on.text = '- flight arrival time\n2. "booking confirmation"\nairline name\nhotel address'
    r = await post("/os/assist/reach", REACH)
    assert r.json() == {"queries": ["flight arrival time", "booking confirmation", "airline name"]}
    assert on.calls[0]["max_tokens"] == ac.REACH.max_tokens
    assert "<above>" in on.calls[0]["user"] and on.calls[0]["user"].endswith("It lands at ⟦cursor⟧\n</draft>")


@pytest.mark.parametrize("text", ["none", "None\n", "", "  \n"])
async def test_reach_needs_nothing_is_an_empty_list(on, text):
    on.text = text
    assert (await post("/os/assist/reach", REACH)).json() == {"queries": []}


async def test_reach_with_no_sentence_is_free(on):
    assert (await post("/os/assist/reach", REACH | {"before": ""})).json() == {"queries": []}
    assert on.calls == []


def test_reach_caps():
    req = ac.ReachRequest.model_validate(
        {"near": "n" * 2000, "before": "x" * 7000 + "END", "after": "a" * 2000 + "Z", "extra": 1}
    )
    assert req.near == "n" * 1500 and len(req.before) == 6000 and req.before.endswith("END")
    assert req.after == "a" * 1000


# --- privacy ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path,body", [("/os/assist/complete", BODY), ("/os/assist/reach", REACH)])
async def test_no_request_or_reply_text_reaches_the_logs(on, caplog, monkeypatch, path, body):
    caplog.set_level(logging.DEBUG)
    on.text = f"#90 thread\n {SECRET} again.\n" if path.endswith("complete") else f"{SECRET} search"
    await post(path, body)
    await post(path, body | {"before": 5})  # a refused body
    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_DAILY_MAX", 0)
    await post(path, body)

    async def fail(*a, **k):
        raise ac.ProviderError("status_500")

    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_DAILY_MAX", 100)
    monkeypatch.setattr(on, "complete", fail)
    await post(path, body)
    logged = "\n".join(r.getMessage() + str(r.args) + str(r.exc_info) for r in caplog.records)
    assert "autocomplete" in logged  # the count line is there
    for word in ("Zanzibar", "7731", "Tuesday", "flight", "lands"):
        assert word not in logged


# --- the OpenAI provider, against a mocked HTTP transport (no network) -----------------------------------------


def _openai(monkeypatch, *responses: httpx.Response) -> tuple[ac.OpenAIProvider, list[dict]]:
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    sent: list[dict] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ac.OpenAIProvider.URL
        sent.append(json.loads(request.content))
        return queue.pop(0)

    p = ac.OpenAIProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p, sent


async def test_openai_request_asks_not_to_store_and_hashes_the_person(monkeypatch):
    reply = {
        "choices": [
            {
                "message": {"content": "#80 message\n ok"},
                "logprobs": {"content": [{"token": "#", "logprob": -0.1, "bytes": [35]}]},
            }
        ],
        "usage": {"prompt_tokens": 900, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 512}},
    }
    p, sent = _openai(monkeypatch, httpx.Response(200, json=reply))
    comp = await p.complete("dev", "user", 80, ac.cache_key("user_a"), 4.0)
    body = sent[0]
    assert body["store"] is False and body["reasoning_effort"] == "none" and body["logprobs"] is True
    assert body["model"] == settings.SPARK_AUTOCOMPLETE_MODEL and body["max_completion_tokens"] == 80
    assert body["prompt_cache_key"] == body["safety_identifier"] == ac.cache_key("user_a")
    assert [m["role"] for m in body["messages"]] == ["developer", "user"]
    assert (comp.input_tokens, comp.cached_tokens, comp.output_tokens) == (900, 512, 5)
    assert comp.token_logprobs == [(1, -0.1)]


async def test_openai_refusing_logprobs_is_asked_once_more_without_them(monkeypatch):
    ok = {"choices": [{"message": {"content": "#50 message\n ok"}}], "usage": {}}
    refused = {"error": {"message": "logprobs are not supported with reasoning models."}}
    p, sent = _openai(
        monkeypatch, httpx.Response(400, json=refused), httpx.Response(200, json=ok), httpx.Response(200, json=ok)
    )
    comp = await p.complete("d", "u", 80, "k", 4.0)
    await p.complete("d", "u", 80, "k", 4.0)
    assert comp.token_logprobs is None and p.logprobs is False
    assert ["logprobs" in b for b in sent] == [True, False, False]


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_openai_errors_carry_only_a_code(monkeypatch, status):
    p, _ = _openai(monkeypatch, httpx.Response(status, json={"error": {"message": f"echo {SECRET}"}}))
    with pytest.raises(ac.ProviderError) as e:
        await p.complete("d", "u", 80, "k", 4.0)
    assert str(e.value) == f"status_{status}"


# --- what each request carried, in the log line --------------------------------------------------------------


def _line(caplog, route: str) -> dict[str, str]:
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"autocomplete {route} ")]
    assert len(lines) == 1, lines
    return dict(kv.split("=", 1) for kv in lines[0].split()[2:])


async def test_the_complete_line_counts_every_part_and_logs_no_text(on, caplog):
    caplog.set_level(logging.INFO)
    on.text = f"#80 memory\n {SECRET} sure."
    body = BODY | {
        "screen": "PLAIN window ⟦field⟧\n\nTo: Sam\nSubject: Hi",
        "screen_structured": f"‹list›\n[Them:]\n{SECRET}\n‹/›\n⟦field⟧",
        "memory": [{"text": "m" * 100}, {"text": "n" * 50}],
        "local_memory": [{"text": "l" * 7}],
        "recent_flow": [
            {"text": "f" * 30, "earlier": [{"text": "e" * 5}]},
            {"structured": "s" * 20, "text": "ignored"},
            {"text": "current", "now": True},
        ],
        "voice": [{"text": "v" * 11}],
        "style": ["one two three", "four five six"],
        "mind": [{"text": "x"}],
        "recent_work": ["w"],
        "reaches_for": ["a", "b"],
        "clipboard": {"text": "c" * 9, "ago_s": 3},
        "after": "a" * 4,
        "declined": "no",
    }
    await post(body=body)
    got = _line(caplog, "complete")
    tree = len(body["screen_structured"])
    want = {
        "kind": "cont", "shown": "True", "source": "memory",
        "mem_n": "2", "mem_c": "150", "lmem_n": "1", "lmem_c": "7",
        "flow_n": "2", "flow_c": "55", "screen_c": str(tree), "tree": "1", "form_c": str(len("To: Sam\nSubject: Hi")),
        "voice_n": "1", "voice_c": "11", "style_n": "2", "style_c": "26", "mind_n": "1", "work_n": "1",
        "reach_n": "2", "clip": "1", "clip_c": "9", "conv": "1", "conv_header_c": "3", "conv_recipients_n": "1",
        "before_c": str(len(BODY["before"])), "after_c": "4", "declined": "1", "cache_write_tokens": "0",
    }  # fmt: skip
    assert {k: got[k] for k in want} == want
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for word in ("Zanzibar", "PLAIN", "Sam", "Tuesday", "ignored"):
        assert word not in logged


async def test_a_quiet_answer_logs_no_source_and_the_plain_screen_size(on, caplog):
    caplog.set_level(logging.INFO)
    on.text = "#0"
    await post(body={"before": "Hi ", "screen": "window ⟦field⟧"})
    got = _line(caplog, "complete")
    assert (got["source"], got["tree"], got["screen_c"], got["conv"], got["clip"]) == ("None", "0", "14", "0", "0")


async def test_the_reach_line_counts_its_parts(on, caplog):
    caplog.set_level(logging.INFO)
    on.text = "flight time"
    await post("/os/assist/reach", REACH)
    got = _line(caplog, "reach")
    assert (got["queries"], got["near_c"], got["before_c"], got["after_c"]) == ("1", str(len(REACH["near"])), "12", "0")
    assert (got["app"], got["title"], got["label"], got["recipient"]) == ("1", "1", "1", "1")


async def test_cache_writes_are_read_from_the_usage(monkeypatch):
    reply = {
        "choices": [{"message": {"content": "#80 message\n ok"}}],
        "usage": {"prompt_tokens": 2000, "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1536}},
    }
    p, _ = _openai(monkeypatch, httpx.Response(200, json=reply))
    assert (await p.complete("d", "u", 80, "k", 4.0)).cache_write_tokens == 1536


_OK = {"choices": [{"message": {"content": "#80 message\n sure."}}], "usage": {"prompt_tokens": 1500}}
_CACHE_REFUSED = {"error": {"message": "Unrecognized request argument supplied: prompt_cache_options"}}


async def test_a_refused_cache_field_is_dropped_and_the_same_request_still_answers(monkeypatch, caplog):
    p, sent = _openai(
        monkeypatch,
        httpx.Response(400, json=_CACHE_REFUSED),
        httpx.Response(200, json=_OK),
        httpx.Response(200, json=_OK),
    )
    caplog.set_level(logging.WARNING)
    first = await p.complete("dev", [f"A {SECRET}\n\n", "B\n\n", "C"], 80, "k", 4.0)
    assert first.text and first.cache_fields is False and p.cache_fields is False
    assert "prompt_cache_options" in sent[0] and isinstance(sent[0]["messages"][1]["content"], list)
    for b in sent[1:2]:  # the retry: the same text, one plain string, no cache fields
        assert "prompt_cache_options" not in b and b["messages"][1]["content"] == f"A {SECRET}\n\nB\n\nC"
    later = await p.complete("dev", ["A\n\n", "C"], 80, "k", 4.0)  # and for the life of the process
    assert later.cache_fields is False and "prompt_cache_options" not in sent[2]
    assert sent[2]["messages"][1]["content"] == "A\n\nC"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "prompt-cache" in warnings[0] and "Zanzibar" not in warnings[0]


async def test_a_breakpoint_refusal_is_dropped_too(monkeypatch):
    refused = {"error": {"message": "Unknown parameter: 'messages[1].content[0].prompt_cache_breakpoint'."}}
    p, sent = _openai(monkeypatch, httpx.Response(400, json=refused), httpx.Response(200, json=_OK))
    assert (await p.complete("dev", ["A\n\n", "C"], 80, "k", 4.0)).text
    assert len(sent) == 2 and sent[1]["messages"][1]["content"] == "A\n\nC"


async def test_a_400_about_something_else_is_still_an_error(monkeypatch):
    other = {"error": {"message": "Invalid value for 'max_completion_tokens'."}}
    p, sent = _openai(monkeypatch, httpx.Response(400, json=other))
    with pytest.raises(ac.ProviderError) as e:
        await p.complete("dev", ["A\n\n", "C"], 80, "k", 4.0)
    assert str(e.value) == "status_400" and len(sent) == 1 and p.cache_fields is True


async def test_the_cache_retry_counts_once_against_the_daily_ceiling(on, monkeypatch, caplog):
    monkeypatch.setattr(settings, "SPARK_AUTOCOMPLETE_DAILY_MAX", 1)
    real, sent = _openai(monkeypatch, httpx.Response(400, json=_CACHE_REFUSED), httpx.Response(200, json=_OK))
    monkeypatch.setattr(ac, "provider", real)
    caplog.set_level(logging.INFO)
    r = await post()
    assert r.status_code == 200 and r.json()["show"] is True and len(sent) == 2
    assert "cache_fields=0" in next(m.getMessage() for m in caplog.records if "autocomplete complete" in m.getMessage())
    assert (await post()).status_code == 429  # the one counted request was the one that came back


async def test_the_log_line_says_when_the_cache_fields_were_sent(on, monkeypatch, caplog):
    real, _ = _openai(monkeypatch, httpx.Response(200, json=_OK))
    monkeypatch.setattr(ac, "provider", real)
    caplog.set_level(logging.INFO)
    await post()
    assert "cache_fields=1" in next(m.getMessage() for m in caplog.records if "autocomplete complete" in m.getMessage())
