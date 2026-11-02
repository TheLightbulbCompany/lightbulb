"""Structured questions use their own fenced response contract, never approval decisions."""

import copy
import json

import pytest

from core.repositories import local_sessions_repo as repo
from core.services import local_sessions as service
from core.services.local_session_relay import _NO_PROMPT, pending_approval, pending_question
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_controls as controls
from tests.unit.services import test_local_session_relay as relay_tests
from tests.unit.services.test_local_sessions import ALICE, BOB, CLI, MAC, OWNER

db = controls.db
transport = controls.transport
QUESTION = {
    "question": "Which release?",
    "header": "Release",
    "multiSelect": False,
    "options": [{"label": "Stable", "description": "Latest tested release"}, {"label": "Preview"}],
}
DIGEST = "a" * 64


def question_event(**changes):
    tagged = dict(
        question=True,
        promptId="question-1",
        inputDigest=DIGEST,
        exact=False,
        modes=[],
        full=True,
        input={"questions": [copy.deepcopy(QUESTION)]},
    )
    tagged.update(changes)
    return {
        **controls.APPROVAL,
        "itemId": "question-1",
        "tool": {**controls.APPROVAL["tool"], "name": "AskUserQuestion", "input": tagged},
    }


def answer(**changes):
    body = dict(
        requestId="answer-1",
        expectedGeneration="generation-1",
        kind="answer",
        promptId="question-1",
        inputDigest=DIGEST,
        answers={QUESTION["question"]: "Custom release"},
    )
    body.update(changes)
    return service.LocalSessionCommand(**body)


async def waiting(transport):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    relay_tests.connect(transport)
    started = await relay_tests.relay(transport).handle_message(
        "conn-1", relay_tests.registration(provider="claude", providerVersion="2.1.50")
    )
    sid = started["sessionId"]
    await controls.waiting(transport, sid, question_event())
    return sid


def test_question_cannot_become_generic_approval_even_if_metadata_is_malformed():
    event = service.LocalSessionEventBody.model_validate
    for changes in ({}, {"question": False}, {"promptId": None}, {"full": False}):
        assert pending_approval([event(question_event(**changes))]) is None
    disguised = copy.deepcopy(controls.APPROVAL)
    disguised["tool"]["name"] = "AskUserQuestion"
    assert pending_approval([event(disguised)]) is None


def test_only_last_full_valid_question_is_answerable():
    event = service.LocalSessionEventBody.model_validate
    assert pending_question([event({"kind": "text", "role": "assistant", "text": "hi"})]) is _NO_PROMPT
    assert pending_question([event(question_event())])["questions"] == [QUESTION]
    for changes in (
        {"full": False},
        {"question": False},
        {"promptId": "another"},
        {"inputDigest": "invalid"},
        {"input": {}},
        {"input": {"questions": [QUESTION, QUESTION]}},
        {"input": {"questions": [{**QUESTION, "multiSelect": "false"}]}},
        {"input": {"questions": [{**QUESTION, "options": []}]}},
        {"input": {"questions": [{**QUESTION, "options": [{"label": "a"}, {"label": "a"}]}]}},
    ):
        assert pending_question([event(question_event()), event(question_event(**changes))]) is None
    assert pending_question([event(question_event()), event(controls.APPROVAL)]) is None


def test_answer_wire_is_separate_and_bounded():
    wire = answer().model_dump(by_alias=True)
    assert wire["answers"] == {"Which release?": "Custom release"}
    assert "approvalId" not in wire and "decision" not in wire
    for changes in (
        {"text": "x"},
        {"decision": "allow"},
        {"approvalId": "approval-1"},
        {"mode": "plan"},
        {"shownDigest": DIGEST},
        {"expectedTurnId": "turn-1"},
        {"promptId": None},
        {"inputDigest": None},
        {"answers": {}},
        {"answers": {str(i): "x" for i in range(5)}},
        {"answers": {"Which release?": "é" * 1000}},
        {"answers": {"Which release?": "\x1b[31m"}},
    ):
        with pytest.raises(ValueError):
            answer(**changes)
    for kind in ("send", "interrupt", "end", "approve"):
        with pytest.raises(ValueError):
            answer(kind=kind)


@pytest.mark.asyncio
async def test_question_projects_answers_without_terminal_and_dispatches_exact_custom_answer(transport):
    sid = await waiting(transport)
    public = (await service.get_session(ALICE, sid)).model_dump(by_alias=True)
    assert "pendingApproval" not in public
    assert public["pendingQuestion"] == {"promptId": "question-1", "inputDigest": DIGEST, "questions": [QUESTION]}
    with pytest.raises(ApiProblem, match="owner"):
        await service.submit_command(BOB, None, sid, answer())
    with pytest.raises(ApiProblem, match="waiting"):
        await service.submit_command(ALICE, None, sid, controls.approve())
    receipt = await service.submit_command(ALICE, None, sid, answer())
    assert receipt.answers == {"Which release?": "Custom release"}
    await relay_tests.relay(transport).dispatch_session(OWNER, sid)
    command = transport[3][-1][1]["command"]
    assert command["kind"] == "answer" and command["answers"] == receipt.answers
    # Retrying an acknowledged response is idempotent; changing it under the same ID is refused.
    assert (await service.submit_command(ALICE, None, sid, answer())).command_seq == receipt.command_seq
    with pytest.raises(ApiProblem, match="different request"):
        await service.submit_command(ALICE, None, sid, answer(answers={QUESTION["question"]: "Preview"}))


@pytest.mark.asyncio
async def test_question_answers_refence_generation_prompt_digest_and_exact_keys(transport):
    sid = await waiting(transport)
    for i, changes in enumerate(
        (
            {"expectedGeneration": "old"},
            {"promptId": "old"},
            {"inputDigest": "b" * 64},
            {"answers": {"Wrong question": "Stable"}},
        )
    ):
        with pytest.raises(ApiProblem):
            await service.submit_command(ALICE, None, sid, answer(requestId=f"bad-{i}", **changes))
    await service.submit_command(ALICE, None, sid, answer())
    await relay_tests.relay(transport).handle_message("conn-1", relay_tests.heartbeat(sid, status="busy"))
    assert "pendingQuestion" not in await repo.get_session(OWNER, sid)
    await relay_tests.relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "answer-1"))["state"] == "rejected"


@pytest.mark.asyncio
async def test_question_replaced_by_approval_clears_question_and_sharing_off_drops_it(transport):
    sid = await waiting(transport)
    await controls.waiting(transport, sid, controls.APPROVAL, seq=1)
    row = await repo.get_session(OWNER, sid)
    assert "pendingQuestion" not in row and "pendingApproval" in row
    await controls.waiting(transport, sid, question_event(), seq=2)
    await service.set_sharing(ALICE, sid, False)
    assert "pendingQuestion" not in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
async def test_answer_rejected_when_control_unverified_and_question_dropped_on_generation_change(transport):
    sid = await waiting(transport)
    row = await repo.get_session(OWNER, sid)
    for blocked in (
        {"controlUnavailableReason": "unsupported-version"},
        {"unshared": True},
        {"status": "offline"},
    ):
        with pytest.raises(ApiProblem):
            service._require_control({**row, **blocked}, answer())
    # A held question is an independent answer route; it does not authorize ordinary follow-ups.
    no_send = {**row, "capabilitiesVerified": False, "capabilities": {**row["capabilities"], "sendMode": "none"}}
    assert service._session_public(no_send, ALICE.user_id).pending_question is not None
    service._require_control(no_send, answer())
    with pytest.raises(ApiProblem):
        service._require_control(no_send, controls.send())
    with pytest.raises(ApiProblem):
        service._require_control(no_send, controls.approve())
    for reason in ("history-gap", "runtime-unverified"):
        recovering = {**no_send, "controlUnavailableReason": reason}
        assert service._session_public(recovering, ALICE.user_id).pending_question is not None
        service._require_control(recovering, answer())
        for wrong in (answer(promptId="old"), answer(inputDigest="b" * 64), controls.send(), controls.approve()):
            with pytest.raises(ApiProblem):
                service._require_control(recovering, wrong)
    await relay_tests.relay(transport).handle_message(
        "conn-1",
        {
            **relay_tests.registration(provider="claude", providerVersion="2.1.50", generation="generation-2"),
            "previousGeneration": "generation-1",
        },
    )
    assert "pendingQuestion" not in await repo.get_session(OWNER, sid)


def test_answer_size_is_utf8_json_bytes():
    small = {"Which release?": "é" * 970}
    assert len(json.dumps(small, ensure_ascii=False, separators=(",", ":")).encode()) < 2000
    assert answer(answers=small).answers == small


def test_full_question_source_fits_when_projection_adds_metadata():
    import json

    source = {
        "questions": [
            {"question": "Choose a path", "options": [{"label": str(i), "description": "x" * 1600} for i in range(6)]}
        ]
    }
    base = len(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    source["questions"][0]["options"][-1]["description"] += "x" * (10 * 1024 - 5 - base)
    projected = pending_question([service.LocalSessionEventBody.model_validate(question_event(input=source))])
    assert projected is not None and projected is not _NO_PROMPT
    assert projected["questions"][0]["question"] == "Choose a path"


def test_unanswerable_question_key_envelope_is_not_advertised():
    source = {"questions": [{"question": str(i) + "x" * 550, "options": [{"label": "Yes"}]} for i in range(4)]}
    assert pending_question([service.LocalSessionEventBody.model_validate(question_event(input=source))]) is None
