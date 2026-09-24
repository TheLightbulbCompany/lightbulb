"""Compatibility shims for third-party MCP servers, applied to the MCP SDK Hermes runs in-process."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def without_empty_meta(message):
    """The same JSON-RPC message minus `params._meta` when that is exactly `{}`; anything else,
    a non-empty `_meta` included, goes out untouched."""
    params = getattr(message, "params", None)
    if isinstance(params, dict) and params.get("_meta") == {}:
        return message.model_copy(update={"params": {k: v for k, v in params.items() if k != "_meta"}})
    return message


def install() -> bool:
    """Wrap the SDK's single outbound write so every request passes through without_empty_meta.

    ponytail: Meta's hosted ads MCP (https://mcp.facebook.com/ads) answers a spec-valid empty
    `"_meta": {}` with 400 InvalidRequest `"meta" for Request must be an dict or null` (seen
    2026-09-24, fbtrace An21-3v3r39S3bhEszSL887), and mcp 2.0's JSONRPCDispatcher always puts
    one on the wire (SEP-414), so the founder's Meta Ads never connected on Hermes. An absent
    `_meta` means the same as an empty one to every other server. Delete this shim once Meta's
    validator accepts `{}`."""
    try:
        from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
    except ImportError:  # not in this environment (unit tests without the SDK)
        return False
    original = JSONRPCDispatcher._write
    if getattr(original, "_lightbulb_empty_meta", False):
        return True

    async def _write(self, message, metadata=None):
        return await original(self, without_empty_meta(message), metadata)

    _write._lightbulb_empty_meta = True
    JSONRPCDispatcher._write = _write
    log.info("mcp compat: empty _meta is dropped from outbound requests")
    return True
