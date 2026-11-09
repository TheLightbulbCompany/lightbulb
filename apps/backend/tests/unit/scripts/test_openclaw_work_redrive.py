"""Dropping a DLQ pointer without repairing the delivery cursor strands every
message queued behind it, so the arithmetic here is the load-bearing part."""

import json
import subprocess

import pytest

from scripts import openclaw_work_redrive as mod


class FakeRepo:
    def __init__(self, last_seq: int | None, *, cas_ok: bool = True):
        self.last_seq = last_seq
        self.cas_ok = cas_ok
        self.calls: list[tuple] = []

    def get_agent_delivery(self, room_id, agent_id):
        return None if self.last_seq is None else {"lastSeq": self.last_seq}

    def advance_agent_delivery(self, room_id, agent_id, expected, through):
        self.calls.append((room_id, agent_id, expected, through))
        return self.cas_ok


def test_cursor_advances_from_the_observed_seq_to_the_stuck_pointer():
    repo = FakeRepo(7)

    assert mod.repair_cursor(repo, "room-1", "agent-1", 11) == "advanced 7->11"
    # The CAS is pinned to the seq we read, so a concurrent drain loses instead
    # of being silently overwritten.
    assert repo.calls == [("room-1", "agent-1", 7, 11)]


def test_cursor_already_past_the_pointer_is_left_alone():
    repo = FakeRepo(11)

    assert mod.repair_cursor(repo, "room-1", "agent-1", 11) == "already_past"
    assert repo.calls == []


def test_missing_cursor_is_reported_not_created():
    """No cursor means the agent left the room; writing one back would
    resurrect delivery state for a membership that no longer exists."""
    repo = FakeRepo(None)

    assert mod.repair_cursor(repo, "room-1", "agent-1", 11) == "no_cursor"
    assert repo.calls == []


def test_lost_cas_is_reported():
    repo = FakeRepo(7, cas_ok=False)

    assert mod.repair_cursor(repo, "room-1", "agent-1", 11) == "cas_conflict"


def _stream_body(payload: dict) -> str:
    return json.dumps({"dynamodb": {"NewImage": {"payload": {"M": payload}}}})


def test_pointer_parses_out_of_the_forwarded_stream_record():
    pointer = mod.parse_pointer(
        _stream_body(
            {
                "kind": {"S": "room_dispatch"},
                "roomId": {"S": "room-1"},
                "agentId": {"S": "agent-1"},
                "throughSeq": {"N": "11"},
            }
        )
    )

    assert pointer == {"kind": "room_dispatch", "roomId": "room-1", "agentId": "agent-1", "throughSeq": "11"}
    assert mod.cursor_target(pointer) == ("room-1", "agent-1", 11)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": {"S": "runtime_operation"}, "ownerId": {"S": "o"}, "operationId": {"S": "op"}},
        {"kind": {"S": "room_dispatch"}, "roomId": {"S": "room-1"}},
        {"kind": {"S": "room_dispatch"}, "roomId": {"S": "r"}, "agentId": {"S": "a"}, "throughSeq": {"S": "junk"}},
    ],
)
def test_pointers_without_a_repairable_cursor_are_skipped(payload):
    assert mod.cursor_target(mod.parse_pointer(_stream_body(payload))) is None


def _fake_aws(commands, *, pointer, cursor_conflict=False):
    """Stand in for the `aws` CLI, recording every command it is handed."""

    def run(*args):
        commands.append(args)
        if args[:2] == ("dynamodb", "update-item") and cursor_conflict:
            raise subprocess.CalledProcessError(
                254, "aws", stderr="An error occurred (ConditionalCheckFailedException)"
            )
        if args[:2] == ("sqs", "get-queue-url"):
            return {"QueueUrl": f"https://sqs.test/{args[3]}"}
        if args[:2] == ("sqs", "receive-message"):
            if len([c for c in commands if c[:2] == ("sqs", "receive-message")]) > 1:
                return {}
            return {
                "Messages": [
                    {
                        "MessageId": "m-1",
                        "ReceiptHandle": "rh-1",
                        "Body": _stream_body(pointer),
                        "Attributes": {"MessageGroupId": "group-1", "ApproximateReceiveCount": "360"},
                    }
                ]
            }
        if args[:2] == ("dynamodb", "get-item"):
            if "META" in args[-1] or "ownerId" in args:
                return {"Item": {"ownerId": {"S": "owner-1"}}}
            return {"Item": {"lastSeq": {"N": "7"}}}
        return {}

    return run


_ROOM_POINTER = {
    "kind": {"S": "room_dispatch"},
    "roomId": {"S": "room-1"},
    "agentId": {"S": "agent-1"},
    "throughSeq": {"N": "11"},
}

_VOICE_USAGE_POINTER = {
    "kind": {"S": "voice_usage"},
    "ownerId": {"S": "org_1"},
    "transactionId": {"S": "meeting-1"},
}


@pytest.mark.parametrize("mode", ["--list", "--redrive", "--drop"])
def test_every_mode_is_read_only_without_apply(monkeypatch, capsys, mode):
    """This tool is pointed at the PROD DLQ; a dry run must never send, delete
    or move a cursor."""
    commands: list[tuple] = []
    monkeypatch.setattr(mod, "aws", _fake_aws(commands, pointer=_ROOM_POINTER))

    assert mod.main(["--env", "prod", mode]) == 0

    verbs = {args[:2] for args in commands}
    assert ("sqs", "send-message") not in verbs
    assert ("sqs", "delete-message") not in verbs
    assert ("dynamodb", "update-item") not in verbs
    output = capsys.readouterr().out
    assert "[dry-run]" in output
    assert "owner=owner-1" in output and "seq=11" in output


def test_drop_with_apply_repairs_the_cursor_then_deletes(monkeypatch, capsys):
    commands: list[tuple] = []
    monkeypatch.setattr(mod, "aws", _fake_aws(commands, pointer=_ROOM_POINTER))

    assert mod.main(["--env", "prod", "--drop", "--apply"]) == 0

    verbs = [args[:2] for args in commands]
    assert verbs.index(("dynamodb", "update-item")) < verbs.index(("sqs", "delete-message"))
    update = next(args for args in commands if args[:2] == ("dynamodb", "update-item"))
    expression = update[update.index("--update-expression") + 1]
    assert "lastDeliveredSeq = if_not_exists(lastDeliveredSeq, :expected)" in expression
    assert "advanced 7->11" in capsys.readouterr().out


def test_drop_keeps_the_pointer_when_the_cursor_repair_loses_the_cas(monkeypatch, capsys):
    """The cursor did NOT move, so deleting the pointer would strand the room
    permanently. Leave it on the DLQ and let the operator re-run."""
    commands: list[tuple] = []
    monkeypatch.setattr(mod, "aws", _fake_aws(commands, pointer=_ROOM_POINTER, cursor_conflict=True))

    assert mod.main(["--env", "prod", "--drop", "--apply"]) == 0

    assert ("sqs", "delete-message") not in [args[:2] for args in commands]
    output = capsys.readouterr().out
    assert "cas_conflict" in output
    assert "kept on the DLQ" in output


def test_redrive_with_apply_keeps_the_group_and_mints_a_new_dedup_id(monkeypatch):
    commands: list[tuple] = []
    monkeypatch.setattr(mod, "aws", _fake_aws(commands, pointer=_ROOM_POINTER))

    assert mod.main(["--env", "prod", "--redrive", "--apply"]) == 0

    send = next(args for args in commands if args[:2] == ("sqs", "send-message"))
    assert send[send.index("--message-group-id") + 1] == "group-1"
    # The original dedup id is still inside the FIFO's 5-minute window.
    assert send[send.index("--message-deduplication-id") + 1] != "group-1"
    assert ("sqs", "delete-message") in [args[:2] for args in commands]


def test_voice_usage_dlq_pointer_can_be_redriven_but_never_dropped(monkeypatch, capsys):
    commands: list[tuple] = []
    monkeypatch.setattr(mod, "aws", _fake_aws(commands, pointer=_VOICE_USAGE_POINTER))

    assert mod.main(["--env", "dev", "--drop", "--apply"]) == 0

    assert ("sqs", "delete-message") not in [args[:2] for args in commands]
    assert "must be redriven, not dropped" in capsys.readouterr().out


def test_voice_usage_dlq_description_includes_the_event_identity():
    pointer = mod.parse_pointer(_stream_body(_VOICE_USAGE_POINTER))
    assert mod.describe(pointer) == "kind=voice_usage owner=org_1 transaction=meeting-1"


def test_undeserializable_pointer_still_describes_itself():
    """These are the ones that actually reach the DLQ — listing must not crash
    on the exact shape the strict deserializer rejected."""
    assert mod.parse_pointer("not-a-stream-record") == {}
    assert "kind=unknown" in mod.describe({})
    assert mod.cursor_target({}) is None
