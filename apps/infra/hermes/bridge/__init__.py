"""Shared env config for the Hermes-on-AgentCore bridge. server/turn/sync import from here."""
from __future__ import annotations

import json
import os
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
# This session's own AWS credentials (backend runtime_transport._session_credentials_header): an
# STS set whose session policy reaches only this owner's ``<owner>/*`` objects and memory. Set by
# server.handle_plugin from the X-Lightbulb-Aws-Session header on each invoke. Module state, never
# os.environ, for the vault token's reason. Read through aws_session_kwargs().
AWS_SESSION: dict = {}
AWS_SESSION_HEADER = "X-Lightbulb-Aws-Session"


def default_s3_prefix(owner: str) -> str:
    """Owner first. The execution role grants `<owner>/*` on the replicas bucket, so a
    prefix beginning with a literal `hermes/` is outside the grant — the old default was
    backwards and only the runtime env override kept it from AccessDenying."""
    return f"{owner}/hermes"


S3_PREFIX = os.environ.get("S3_PREFIX", default_s3_prefix(OWNER)).strip("/")
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


def set_aws_session(raw: str) -> None:
    """Keep the invoke's scoped credentials; a malformed header is ignored (the last set stays)."""
    global AWS_SESSION
    try:
        creds = json.loads(raw)
        expires = datetime.fromisoformat(creds["expiration"])
        keys = (creds["accessKeyId"], creds["secretAccessKey"], creds["sessionToken"])
    except (ValueError, TypeError, KeyError):
        return
    if all(isinstance(k, str) and k for k in keys):
        AWS_SESSION = {"keys": keys, "expires": expires}


def aws_session_kwargs() -> dict:
    """boto3 client kwargs for the scoped credentials, or {} (the execution role, today's path)
    when no invoke brought any or they have expired."""
    creds = AWS_SESSION
    if not creds or creds["expires"] <= datetime.now(timezone.utc):
        return {}
    access, secret, token = creds["keys"]
    return {"aws_access_key_id": access, "aws_secret_access_key": secret, "aws_session_token": token}
