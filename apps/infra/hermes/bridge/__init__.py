"""Shared env config for the Hermes-on-AgentCore bridge. server/turn/sync import from here."""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PORT = int(os.environ.get("AGENTCORE_PORT", "8080"))
HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
OWNER = os.environ.get("ISOL8_OWNER_ID", "")
API = os.environ.get("ISOL8_API_URL", "").rstrip("/")
HOOK_TOKEN = os.environ.get("OPENCLAW_HOOK_TOKEN", "")
# The bridge's own inbound credential: module state ONLY. In os.environ every agent's shell would
# inherit it and could drive the loopback /invocations with it (audit P1-3), so any env copy is
# dropped here, and the boot fetch below never mirrors one back.
GATEWAY_TOKEN = os.environ.pop("GATEWAY_TOKEN", "")
# The website-login vault's credential (backend ``website_logins`` internal routes). Set ONLY by
# server.handle_plugin from the X-Lightbulb-Vault header the backend puts on every invoke, and
# held here -- never os.environ (the agent's shell inherits that, hook token included), never a
# file, never the boot secret (the execution role can read it, and the shell can read the role's
# credentials over the microVM metadata service). Read it as ``bridge.VAULT_TOKEN`` at call time.
VAULT_TOKEN = ""
S3_BUCKET = os.environ.get("S3_BUCKET", "")
# The workspace's network member key (base64 of a 32-byte Ed25519 seed), from the boot secret
# only. Module state, never os.environ: bridge.network writes it as the sidecar's identity.key.
NETWORK_KEY = ""


def default_s3_prefix(owner: str) -> str:
    """Owner first. The execution role grants `<owner>/*` on the replicas bucket, so a
    prefix beginning with a literal `hermes/` is outside the grant — the old default was
    backwards and only the runtime env override kept it from AccessDenying."""
    return f"{owner}/hermes"


S3_PREFIX = os.environ.get("S3_PREFIX", default_s3_prefix(OWNER)).strip("/")

#: The backend names the tenant on every invoke (runtime_transport). A per-owner runtime boots
#: with ISOL8_OWNER_ID already set; a shared one boots with none and takes the first invoke's.
TENANT_HEADER = "X-Lightbulb-Tenant"
_TENANT_LOCK = threading.Lock()


def bind_tenant(owner: str | None) -> bool:
    """Whether an invoke for ``owner`` may run in this session. AgentCore gives every session its
    own microVM, so this process serves one tenant: the boot env's, or else the first invoke's.
    Any other tenant is refused. No header (an older backend) runs as before."""
    global OWNER, S3_PREFIX
    if not owner:
        return True
    with _TENANT_LOCK:
        if OWNER:
            return owner == OWNER
        old_prefix = S3_PREFIX
        OWNER = owner
        # Always from the bound owner: a prefix in a shared runtime's env would put every tenant
        # under one prefix.
        S3_PREFIX = default_s3_prefix(owner)
        os.environ["ISOL8_OWNER_ID"] = owner  # the memory plugin and the skill scripts read it
        # Every bridge module did `from bridge import OWNER` at import, before any invoke, so each
        # holds the empty boot value: re-point those copies (and sync's prefix) once, here.
        for name, mod in list(sys.modules.items()):
            if name.startswith("bridge.") and mod is not None:
                if getattr(mod, "OWNER", None) == "":
                    mod.OWNER = owner
                if getattr(mod, "S3_PREFIX", None) == old_prefix:
                    mod.S3_PREFIX = S3_PREFIX
        return True
STARTED = time.time()

# A workspace with an AgentCore Browser profile browses its shared session through the bridge's
# loopback CDP multiplexer (cdp_mux; browser_control.install gives each run its own endpoint), and
# works its screen through Hermes's computer_use with our driver. Without a profile, neither is set,
# so Hermes advertises no browser tools and no computer_use. Old runtime specs may still carry the
# v1 provider; it is ignored when the workspace has a profile.
if os.environ.get("AGENTCORE_PROFILE_ID"):
    os.environ.pop("AGENT_BROWSER_PROVIDER", None)
    os.environ.setdefault("BROWSER_CDP_URL", "ws://127.0.0.1:9222")
    os.environ.setdefault("HERMES_CUA_DRIVER_CMD", "/opt/lightbulb/bin/lightbulb-cua")


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# The runtime's environmentVariables are plaintext to anyone with GetAgentRuntime (spec §4.9
# "Secrets"), so they carry only this one Secrets Manager id, never the tokens themselves.
# HOOK_TOKEN/GATEWAY_TOKEN are mutable module state so this overwrites them in place -- before
# `server`/`turn` do `from bridge import HOOK_TOKEN`/`GATEWAY_TOKEN`, so what they capture at
# their own import is already the boot-fetched value. Dev scripts that never set
# LIGHTBULB_SECRET_ID keep using the plain env-var tokens above, unchanged.
_secret_id = os.environ.get("LIGHTBULB_SECRET_ID", "")
if _secret_id:
    from bridge.sync import load_boot_secrets as _load_boot_secrets

    _boot_secrets = _load_boot_secrets(_secret_id)
    HOOK_TOKEN = _boot_secrets.get("hookToken") or HOOK_TOKEN
    GATEWAY_TOKEN = _boot_secrets.get("gatewayToken") or GATEWAY_TOKEN
    NETWORK_KEY = _boot_secrets.get("networkKey") or ""
    # M2-R12: a reader that is not turn-scoped has no per-turn context file to take a hook
    # token from and reads the process env directly, the same way this module's own
    # module-level defaults above do. Mirror the boot-fetched hook token into the process env so
    # those reads see it too. GATEWAY_TOKEN is deliberately NOT mirrored (see above).
    os.environ["OPENCLAW_HOOK_TOKEN"] = HOOK_TOKEN
    if _boot_secrets.get("bedrockBearer"):
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = _boot_secrets["bedrockBearer"]
    # providerSwitchFrozen/providerSwitchGeneration used to be exported here for turn.py's
    # freeze gate and server.py's generation check -- both retired (Task 11). A provider switch
    # is now just a profile rewrite the bridge picks up on its next turn; an old boot secret
    # that still carries these keys is simply ignored.

# Task 13b: mark this process as a profile multiplexer once, at boot -- agent.secret_scope's
# get_secret() then fails CLOSED (raises) for any direct caller that reads a credential with no
# per-turn scope installed (turn._secret_scope, see turn.py), instead of silently serving another
# profile's os.environ value. No-op outside the real Hermes image (dev/test environments).
# Minor 9 (fix round 1 continuation): this also means any FUTURE process-wide credential this
# module starts injecting into os.environ (like the boot secrets just above) is invisible to a
# multiplexed turn's ${VAR} refs -- a value a secondary profile's config.yaml needs to resolve
# must land in that profile's own .env (hermes_profiles.build_profile_env), not os.environ.
try:
    from agent.secret_scope import set_multiplex_active as _set_multiplex_active

    _set_multiplex_active(True)
except ImportError:
    pass
