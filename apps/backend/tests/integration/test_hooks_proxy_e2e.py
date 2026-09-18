"""End-to-end smoke for the hooks proxy -- REMOVED.

This file used to boot a stub HTTP server posing as the user's OpenClaw container and POST a
real request through the FastAPI app's TestClient, exercising the proxy's raw httpx forward
(`GATEWAY_PORT`-addressed) end to end: body pass-through, the audit row, and the
X-Forwarded-*/Forwarded/X-Real-IP stripping guarantee OpenClaw 8.1's ingress attribution
depends on.

That mechanism is dead code now (deletion PR-1): `runtime_transport.host_for` is pinned to
"agentcore" fleet-wide, so `routers/hooks_proxy.py` always takes the `runtime_transport.request`
branch and never dials a real socket at `GATEWAY_PORT`. A real end-to-end equivalent for the
AgentCore transport would mean standing up a fake `bedrock-agentcore` boto3 endpoint behind
`invoke_agent_runtime` -- a materially different test, not an adaptation of this one -- so it
was not rebuilt here. The same header-stripping/body-forwarding guarantee this test protected
is covered at the unit level (with `runtime_transport.request` faked, the same seam every other
AgentCore test in this repo uses) by
`tests/unit/routers/test_hooks_proxy.py::test_successful_forward_passes_body_and_writes_audit`.
"""
