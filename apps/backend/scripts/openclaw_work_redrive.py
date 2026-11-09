"""Inspect and drain the OpenClaw work DLQ.

The work FIFO carries outbox *pointers* (room dispatch / runtime operation), not
message text, and ``maxReceiveCount`` is 360 — so anything that reaches the DLQ
has exhausted retries. The current queue is configured for 30 receives; this
tool is also used for newer pointer kinds such as durable voice usage events.

Three modes, dry-run unless ``--apply`` is passed::

    uv run python -m scripts.openclaw_work_redrive --env prod --list
    uv run python -m scripts.openclaw_work_redrive --env prod --redrive --apply
    uv run python -m scripts.openclaw_work_redrive --env prod --drop --apply

``--redrive`` sends each pointer back to the main FIFO under its ORIGINAL
``MessageGroupId`` (ordering per room/agent is the whole point of the FIFO) with
a fresh ``MessageDeduplicationId``, because the original id is still inside the
queue's 5-minute dedup window and a replay would be silently swallowed.

``--drop`` deletes the pointer and, for a ``room_dispatch``, advances that
agent's delivery cursor past the seq it was stuck on — the same compare-and-set
``messaging_repo.advance_agent_delivery`` uses — so the messages queued behind
it flow again. Without the cursor repair, dropping the pointer strands the room.

Shells out to the ``aws`` CLI (boto3's SSO refresh is broken in this venv); the
profile comes from the ambient ``AWS_PROFILE``. No message text is ever read or
printed — only pointer identity.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid

_KIND_LABELS = ("room_dispatch", "runtime_operation", "voice_usage")


def aws(*args: str) -> dict:
    """Run one `aws` CLI command and parse its JSON output ({} when empty)."""
    result = subprocess.run(
        ["aws", *args, "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def parse_pointer(body: str) -> dict[str, str]:
    """Lenient read of the pointer inside the forwarded DynamoDB stream record.

    Deliberately lenient: the strict ``deserialize_pointer`` is what rejected
    these messages in the first place, so listing has to survive whatever shape
    they actually have.
    """
    try:
        payload = json.loads(body)["dynamodb"]["NewImage"]["payload"]["M"]
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: value.get("S") or value.get("N") for key, value in payload.items() if isinstance(value, dict)}


def cursor_target(pointer: dict[str, str]) -> tuple[str, str, int] | None:
    """(room, agent, seq) when this pointer has a delivery cursor to repair."""
    if pointer.get("kind") != "room_dispatch":
        return None
    try:
        return pointer["roomId"], pointer["agentId"], int(pointer["throughSeq"])
    except (KeyError, TypeError, ValueError):
        return None


def repair_cursor(repo, room_id: str, agent_id: str, through: int) -> str:
    """Advance the agent delivery cursor past a dropped pointer's seq."""
    cursor = repo.get_agent_delivery(room_id, agent_id)
    if cursor is None:
        return "no_cursor"
    expected = int(cursor["lastSeq"])
    if expected >= through:
        return "already_past"
    if repo.advance_agent_delivery(room_id, agent_id, expected, through):
        return f"advanced {expected}->{through}"
    return "cas_conflict"


class CliRepo:
    """The two cursor calls of ``messaging_repo``, over the AWS CLI."""

    def __init__(self, table: str) -> None:
        self.table = table

    def _key(self, room_id: str, agent_id: str) -> str:
        return json.dumps({"pk": {"S": f"ROOM#{room_id}"}, "sk": {"S": f"DELIVERY#AGENT#{agent_id}"}})

    def get_agent_delivery(self, room_id: str, agent_id: str) -> dict | None:
        item = aws("dynamodb", "get-item", "--table-name", self.table, "--key", self._key(room_id, agent_id)).get(
            "Item"
        )
        return {"lastSeq": int(item["lastSeq"]["N"])} if item else None

    def advance_agent_delivery(self, room_id: str, agent_id: str, expected: int, through: int) -> bool:
        try:
            aws(
                "dynamodb",
                "update-item",
                "--table-name",
                self.table,
                "--key",
                self._key(room_id, agent_id),
                "--update-expression",
                "SET lastSeq = :through, lastDeliveredSeq = if_not_exists(lastDeliveredSeq, :expected)",
                "--condition-expression",
                "attribute_exists(sk) AND lastSeq = :expected",
                "--expression-attribute-values",
                json.dumps({":through": {"N": str(through)}, ":expected": {"N": str(expected)}}),
            )
        except subprocess.CalledProcessError as exc:
            if "ConditionalCheckFailed" in (exc.stderr or ""):
                return False
            raise
        return True


def describe(pointer: dict[str, str]) -> str:
    kind = pointer.get("kind")
    if kind not in _KIND_LABELS:
        return f"kind=unknown (undeserializable pointer, keys={sorted(pointer)})"
    if kind == "runtime_operation":
        return f"kind=runtime_operation owner={pointer.get('ownerId')} operation={pointer.get('operationId')}"
    if kind == "voice_usage":
        return f"kind=voice_usage owner={pointer.get('ownerId')} transaction={pointer.get('transactionId')}"
    return (
        f"kind=room_dispatch room={pointer.get('roomId')} "
        f"agent={pointer.get('agentId')} seq={pointer.get('throughSeq')}"
    )


def room_owner(table: str, room_id: str) -> str:
    item = aws(
        "dynamodb",
        "get-item",
        "--table-name",
        table,
        "--key",
        json.dumps({"pk": {"S": f"ROOM#{room_id}"}, "sk": {"S": "META"}}),
        "--projection-expression",
        "ownerId",
    ).get("Item")
    return item["ownerId"]["S"] if item else "<room gone>"


def drain(dlq_url: str, limit: int) -> list[dict]:
    """Receive up to ``limit`` distinct DLQ messages (they stay on the queue)."""
    seen: dict[str, dict] = {}
    while len(seen) < limit:
        batch = (
            aws(
                "sqs",
                "receive-message",
                "--queue-url",
                dlq_url,
                "--max-number-of-messages",
                "10",
                "--wait-time-seconds",
                "2",
                "--visibility-timeout",
                "120",
                "--attribute-names",
                "All",
            ).get("Messages")
            or []
        )
        fresh = [message for message in batch if message["MessageId"] not in seen]
        if not fresh:
            break
        for message in fresh:
            seen[message["MessageId"]] = message
    return list(seen.values())[:limit]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=("dev", "prod"), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="print each DLQ pointer's identity")
    mode.add_argument("--redrive", action="store_true", help="send back to the main FIFO, then delete from the DLQ")
    mode.add_argument("--drop", action="store_true", help="delete from the DLQ and repair the agent delivery cursor")
    parser.add_argument("--apply", action="store_true", help="actually mutate (default: dry run)")
    parser.add_argument("--max", type=int, default=100, help="stop after this many messages (default 100)")
    args = parser.parse_args(argv)

    table = f"lightbulb-{args.env}-messaging"
    queue_url = aws("sqs", "get-queue-url", "--queue-name", f"lightbulb-{args.env}-openclaw-work.fifo")["QueueUrl"]
    dlq_url = aws("sqs", "get-queue-url", "--queue-name", f"lightbulb-{args.env}-openclaw-work-dlq.fifo")["QueueUrl"]

    messages = drain(dlq_url, args.max)
    if not messages:
        print(f"{args.env} DLQ is empty")
        return 0
    prefix = "" if args.apply else "[dry-run] "

    for message in messages:
        deletable = True
        attributes = message.get("Attributes") or {}
        pointer = parse_pointer(message.get("Body", ""))
        owner = room_owner(table, pointer["roomId"]) if pointer.get("roomId") else pointer.get("ownerId", "<none>")
        print(
            f"{prefix}{message['MessageId']} owner={owner} {describe(pointer)} "
            f"group={attributes.get('MessageGroupId')} receives={attributes.get('ApproximateReceiveCount')}"
        )
        if args.list:
            continue

        if args.redrive:
            print(f"{prefix}  redrive -> {queue_url}")
            if args.apply:
                aws(
                    "sqs",
                    "send-message",
                    "--queue-url",
                    queue_url,
                    "--message-body",
                    message["Body"],
                    "--message-group-id",
                    attributes["MessageGroupId"],
                    # The original dedup id is still inside the queue's 5-minute
                    # window; reusing it would drop the replay silently.
                    "--message-deduplication-id",
                    uuid.uuid4().hex,
                )
        elif pointer.get("kind") == "voice_usage":
            # A metering pointer owns financial data and must be redriven, never
            # dropped through the generic maintenance path.
            deletable = False
            print(f"{prefix}  kept on the DLQ: voice usage must be redriven, not dropped")
        elif (target := cursor_target(pointer)) is not None:
            room_id, agent_id, through = target
            repo = CliRepo(table)
            if args.apply:
                outcome = repair_cursor(repo, room_id, agent_id, through)
                # A lost CAS means the cursor moved under us and the repair did
                # NOT happen; deleting now would strand the room for good.
                # no_cursor is different — the agent is gone, nothing to strand.
                deletable = outcome != "cas_conflict"
            else:
                cursor = repo.get_agent_delivery(room_id, agent_id)
                outcome = f"would advance from {cursor['lastSeq']} to {through}" if cursor else "no_cursor"
            print(f"{prefix}  cursor: {outcome}")
        else:
            print(f"{prefix}  cursor: not applicable ({pointer.get('kind', 'unknown')})")

        if args.apply and deletable:
            aws("sqs", "delete-message", "--queue-url", dlq_url, "--receipt-handle", message["ReceiptHandle"])
            print("  deleted from the DLQ")
        elif args.apply:
            print("  kept on the DLQ — cursor unrepaired, re-run to retry")

    print(f"{prefix}{len(messages)} message(s) handled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
