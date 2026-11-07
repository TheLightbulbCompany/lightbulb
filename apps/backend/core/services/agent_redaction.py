"""Single source of truth for redacting secret fields from agent records.

An ``openclaw_gateway`` agent's ``adapterConfig`` carries the operator device's
signing private key (``devicePrivateKeyPem``) and the container gateway token
(``authToken``). EVERY surface that echoes an agent record to a client — the
``/teams`` BFF read/create paths — MUST strip these before they reach the browser/DOM, or anyone with the
response can impersonate the agent's operator device.

Keeping the field set + stripper here (not duplicated per surface) means a
newly-added secret field is redacted everywhere at once — a divergence on a
security-critical constant is exactly how a field silently keeps leaking on one
surface after it's locked down on another.
"""

from __future__ import annotations

# Secret / Isol8-managed fields stripped from any agent record before it reaches
# a client. ``adapterConfig`` holds the device signing key + the gateway token;
# ``adapterType`` is the Isol8-managed adapter mechanism we don't surface.
AGENT_SECRET_FIELDS = frozenset({"adapterConfig", "adapterType"})


def strip_agent_secrets(obj):
    """Recursively drop ``AGENT_SECRET_FIELDS`` keys anywhere in a JSON-like value.

    Recursive (not single-level) so one implementation covers every response
    envelope a surface might echo: a bare agent dict, a list of agents, a
    ``{"agents": [...]}`` wrapper, and any nested ``adapterConfig`` (e.g.
    ``runtimeConfig.modelProfiles.*.adapterConfig``). Non-dict / non-list values
    pass through unchanged, so it's safe to call on any upstream payload shape.
    """
    if isinstance(obj, dict):
        return {k: strip_agent_secrets(v) for k, v in obj.items() if k not in AGENT_SECRET_FIELDS}
    if isinstance(obj, list):
        return [strip_agent_secrets(item) for item in obj]
    return obj
