"""Meta's ads MCP 400s a spec-valid empty `_meta`; the shim drops only an EMPTY one."""
from bridge import mcp_compat


class _Msg:
    """The two members the shim uses from the SDK's pydantic JSONRPCRequest."""

    def __init__(self, params):
        self.params = params

    def model_copy(self, update):
        return _Msg(update["params"])


def test_an_empty_meta_is_dropped_and_a_non_empty_one_is_kept():
    empty = _Msg({"protocolVersion": "2025-11-25", "_meta": {}})
    assert mcp_compat.without_empty_meta(empty).params == {"protocolVersion": "2025-11-25"}
    assert empty.params["_meta"] == {}  # the original message is not mutated

    traced = _Msg({"name": "x", "_meta": {"progressToken": 3, "traceparent": "00-ab"}})
    assert mcp_compat.without_empty_meta(traced) is traced
    no_params = _Msg(None)
    assert mcp_compat.without_empty_meta(no_params) is no_params
