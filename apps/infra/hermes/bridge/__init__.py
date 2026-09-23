"""Shared env config for the Hermes-on-AgentCore bridge. server/turn/sync import from here."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

PORT = int(os.environ.get("AGENTCORE_PORT", "8080"))
HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
OWNER = os.environ.get("ISOL8_OWNER_ID", "")
API = os.environ.get("ISOL8_API_URL", "").rstrip("/")
HOOK_TOKEN = os.environ.get("OPENCLAW_HOOK_TOKEN", "")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")


def default_s3_prefix(owner: str) -> str:
    """Owner first. The execution role grants `<owner>/*` on the replicas bucket, so a
    prefix beginning with a literal `hermes/` is outside the grant — the old default was
    backwards and only the runtime env override kept it from AccessDenying."""
    return f"{owner}/hermes"


S3_PREFIX = os.environ.get("S3_PREFIX", default_s3_prefix(OWNER)).strip("/")
STARTED = time.time()

# Browser v2 keeps lifecycle in the backend and points Hermes at the loopback discovery seam.
# Old runtime specs may still carry the v1 provider; the new bridge ignores it only when the
# workspace has an AgentCore Browser profile, so the image remains backward-compatible.
if os.environ.get("AGENTCORE_PROFILE_ID"):
    os.environ.pop("AGENT_BROWSER_PROVIDER", None)
    os.environ.setdefault("AGENT_BROWSER_CDP", "http://127.0.0.1:8080")


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
    # M2-R12: a reader that is not turn-scoped has no per-turn context file to take a hook
    # token from and reads the process env directly, the same way this module's own
    # module-level defaults above do. Mirror the boot-fetched values into the process env so
    # those reads see them too, not just `from bridge import HOOK_TOKEN`.
    os.environ["OPENCLAW_HOOK_TOKEN"] = HOOK_TOKEN
    os.environ["GATEWAY_TOKEN"] = GATEWAY_TOKEN
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
