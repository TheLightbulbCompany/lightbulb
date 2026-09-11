"""The agent-event transform's branch table.

Anything ``_transform_agent_event`` does not recognise returns ``None`` and is
silently dropped, so a stream we forget to handle disappears with no error
anywhere. These tests pin the branches the chat surface depends on.
"""

from core.gateway.connection_pool import GatewayConnection


def _payload(stream: str, data: dict, run_id: str = "run_1") -> dict:
    return {"stream": stream, "data": data, "runId": run_id}


def _transform(payload: dict):
    """Call through an unbound instance.

    ``_transform_agent_event`` is a ``@staticmethod`` today; a sibling plan
    converts it to an instance method so it can reach ``self._sender``. Calling
    through an instance works under BOTH signatures, so these tests survive
    whichever lands second.
    """
    return GatewayConnection.__new__(GatewayConnection)._transform_agent_event(payload)


def test_run_status_becomes_an_agent_activity_frame():
    out = _transform(_payload("run_status", {"phase": "starting_model"}))
    assert out is not None
    assert out["type"] == "agent_activity"
    assert out["activity"] == "run_status"
    assert out["title"] == "starting_model"
    assert out["detail"] == {"phase": "starting_model"}
    assert out["runId"] == "run_1"


def test_run_status_without_a_phase_is_dropped():
    """A phase-less status event has nothing to show; don't render an empty pill."""
    assert _transform(_payload("run_status", {})) is None


def test_compaction_start_becomes_an_activity_frame():
    out = _transform(_payload("compaction", {"phase": "start"}))
    assert out is not None
    assert out["type"] == "agent_activity"
    assert out["activity"] == "compaction"
    assert out["title"] == "start"


def test_compaction_end_carries_the_outcome_fields():
    """A failed compaction must be distinguishable from a successful one."""
    out = _transform(_payload("compaction", {"phase": "end", "completed": False, "willRetry": True}))
    assert out is not None
    assert out["detail"]["completed"] is False
    assert out["detail"]["willRetry"] is True


def test_unknown_stream_is_still_dropped():
    """Guards against a future edit that makes the transform permissive."""
    assert _transform(_payload("nonsense", {"x": 1})) is None


def test_approval_and_lifecycle_stay_out_of_the_transform():
    """Both are consumed by _handle_message; forwarding them here would fight it."""
    assert _transform(_payload("approval", {"phase": "start"})) is None
    assert _transform(_payload("lifecycle", {"phase": "end"})) is None
