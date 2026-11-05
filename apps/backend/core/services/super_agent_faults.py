"""Dev-only fault hooks for the super-agent (wave-2 O Task 10; spec 09 scenarios).

``SUPER_AGENT_FAULTS`` is a comma-separated list of ``name`` or ``name:param``. ``fault(name)`` returns the param
(``""`` when there is none) while the fault is on, ``None`` otherwise. Always ``None`` in prod, where ``config``
also refuses a non-empty list at boot. Hooks:

- ``temporal_down``: ``core.temporal.client.get_client`` raises ``RPCError(UNAVAILABLE)``.
- ``router_turn_timeout``: ``router_turn`` fails before the run (the workflow's retries, then ``apply_fallback``).
- ``conditional_conflict:<pct>``: the tasks table's conditional writes fail that often.
- ``fake_worker:<ms>``: ``worker_turn`` completes itself after ``ms`` with a canned output; no bridge run.
- ``scripted_router:<s3key>``: decisions keyed by the exact message text, read from that JSON in the owner-state
  bucket, instead of a Router run (``{"<text>": [decision, ...]}``; a text with no entry gets no decision).
"""

from __future__ import annotations

import json
import random

from botocore.exceptions import ClientError
from temporalio.service import RPCError, RPCStatusCode

from core.config import settings
from core.dynamodb import run_in_thread

FAULT_SETTING = "SUPER_AGENT_FAULTS"


def fault(name: str) -> str | None:
    if settings.ENVIRONMENT == "prod":
        return None
    for item in (settings.SUPER_AGENT_FAULTS or "").split(","):
        key, _, param = item.strip().partition(":")
        if key == name:
            return param
    return None


def temporal_down_error() -> RPCError:
    return RPCError("fault: temporal_down", RPCStatusCode.UNAVAILABLE, b"")


def maybe_conflict() -> None:
    """A conditional write that loses its race `pct` percent of the time (the repo's own retry/replay paths)."""
    pct = fault("conditional_conflict")
    if pct is None:
        return
    if random.random() * 100 < float(pct or 100):  # noqa: S311 -- a dev fault, not security
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "fault: conditional_conflict"}},
            "UpdateItem",
        )


def _read_script(key: str) -> dict:
    from core.services.hermes_profiles import _require_owner_bucket, _shared_s3_client

    body = _shared_s3_client().get_object(Bucket=_require_owner_bucket(), Key=key)["Body"].read()
    script = json.loads(body)
    if not isinstance(script, dict):
        raise ValueError("a Router script is a JSON object keyed by message text")
    return script


async def scripted_decisions(key: str, texts: dict[str, str]) -> list[dict]:
    """`texts`: trigger id -> its exact message text. Each matching entry's decisions get that trigger's id."""
    script = await run_in_thread(_read_script, key)
    out: list[dict] = []
    for trigger_id, text in texts.items():
        for decision in script.get(text) or []:
            out.append({**decision, "triggerIds": [trigger_id]})
    return out
