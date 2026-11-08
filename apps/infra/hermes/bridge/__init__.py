"""Shared env config for the Hermes-on-AgentCore bridge. server/turn/sync import from here."""
from __future__ import annotations

import json
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
#: Booted with no owner: a shared runtime, whose execution role has no S3, memory or browser grant.
SHARED = not OWNER

#: The backend names the tenant on every invoke (runtime_transport). A per-owner runtime boots
#: with ISOL8_OWNER_ID already set; a shared one boots with none and takes the first invoke's.
TENANT_HEADER = "X-Lightbulb-Tenant"
#: A shared runtime has no boot secret: the backend sends the owner's boot values (the same keys the
#: boot secret holds, plus the per-owner env a per-owner runtime boots with) on every invoke
#: (runtime_transport._boot_header). Only the binding invoke's are taken. Module state only, like
#: the boot secret's values: never logged, never a file.
BOOT_HEADER = "X-Lightbulb-Boot"
_TENANT_LOCK = threading.Lock()


def bind_tenant(owner: str | None, boot: str | None = None) -> bool:
    """Whether an invoke for ``owner`` may run in this session. AgentCore gives every session its
    own microVM, so this process serves one tenant: the boot env's, or else the first invoke's.
    Any other tenant is refused, and so is an invoke naming none before a shared runtime is bound
    (it would run with no owner: keys outside every prefix). A per-owner runtime still takes an
    older backend's untenanted invoke. The binding invoke's ``boot`` values (BOOT_HEADER) become
    this session's tokens; without a gateway token in them nothing is bound, so a malformed first
    invoke cannot strand the session. Modules read ``bridge.OWNER``/``bridge.HOOK_TOKEN`` and
    ``sync.S3_PREFIX`` at call time, so binding is these assignments and nothing else."""
    global OWNER, S3_PREFIX
    if not owner:
        return bool(OWNER)
    with _TENANT_LOCK:
        if OWNER:
            return owner == OWNER
        if boot is not None and not _adopt_boot(boot):
            return False
        OWNER = owner
        # Always from the bound owner: a prefix in a shared runtime's env would put every tenant
        # under one prefix.
        S3_PREFIX = default_s3_prefix(owner)
        sync = sys.modules.get("bridge.sync")
        if sync is not None:  # sync's own copy is its source of truth (tests re-point it there)
            sync.S3_PREFIX = S3_PREFIX
        os.environ["ISOL8_OWNER_ID"] = owner  # the memory plugin and the skill scripts read it
        return True


def _adopt_boot(raw: str) -> bool:
    """A shared runtime's first invoke: what a per-owner runtime reads from its boot secret and env.
    False, nothing taken, without a gateway token."""
    global HOOK_TOKEN, GATEWAY_TOKEN, NETWORK_KEY
    try:
        boot = json.loads(raw)
    except ValueError:
        return False
    text = {k: v for k, v in boot.items() if isinstance(v, str) and v} if isinstance(boot, dict) else {}
    if "gatewayToken" not in text:
        return False
    HOOK_TOKEN = text.get("hookToken", HOOK_TOKEN)
    GATEWAY_TOKEN = text.get("gatewayToken", GATEWAY_TOKEN)
    NETWORK_KEY = text.get("networkKey", NETWORK_KEY)
    os.environ["OPENCLAW_HOOK_TOKEN"] = HOOK_TOKEN  # as the boot-secret path below
    if "bedrockBearer" in text:
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = text["bedrockBearer"]
    if "browserProfileId" in text:
        # The gate a per-owner env carries with its profile (hermes_provisioning.build_hermes_runtime_env).
        os.environ.update(AGENTCORE_PROFILE_ID=text["browserProfileId"],
                          AGENT_BROWSER_EXECUTABLE_PATH="/opt/lightbulb/no-local-browser")
        _browser_env()
    if boot.get("networkWorkers") is True:
        os.environ["LIGHTBULB_NETWORK_WORKERS"] = "1"
    cap, turn = boot.get("maxConcurrentTurns"), sys.modules.get("bridge.turn")
    if isinstance(cap, int) and cap >= 2 and turn is not None:  # no turn has run yet: resize the slots
        turn.MAX_CONCURRENT_TURNS = cap
        turn._turn_slots = threading.BoundedSemaphore(cap)
        turn._compaction_slots = threading.BoundedSemaphore(cap - 1)
    return True


STARTED = time.time()

# A workspace with an AgentCore Browser profile browses its shared session through the bridge's
# loopback CDP multiplexer (cdp_mux; browser_control.install gives each run its own endpoint), and
# works its screen through Hermes's computer_use with our driver. Without a profile, neither is set,
# so Hermes advertises no browser tools and no computer_use. Old runtime specs may still carry the
# v1 provider; it is ignored when the workspace has a profile.
def _browser_env() -> None:
    if os.environ.get("AGENTCORE_PROFILE_ID"):
        os.environ.pop("AGENT_BROWSER_PROVIDER", None)
        os.environ.setdefault("BROWSER_CDP_URL", "ws://127.0.0.1:9222")
        os.environ.setdefault("HERMES_CUA_DRIVER_CMD", "/opt/lightbulb/bin/lightbulb-cua")


_browser_env()


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
    """Keep the invoke's scoped credentials; a malformed header is ignored (the last set stays).
    So is a set that expires before the one held: each backend task mints its own, and an older
    task's set arriving after a newer one must not shorten the session's credentials."""
    global AWS_SESSION
    try:
        creds = json.loads(raw)
        expires = datetime.fromisoformat(creds["expiration"])
        keys = (creds["accessKeyId"], creds["secretAccessKey"], creds["sessionToken"])
    except (ValueError, TypeError, KeyError):
        return
    if all(isinstance(k, str) and k for k in keys) and (not AWS_SESSION or expires >= AWS_SESSION["expires"]):
        AWS_SESSION = {"keys": keys, "expires": expires}


#: The backend refreshes its set this long before expiry (runtime_transport._SESSION_REFRESH_S).
AWS_SESSION_DUE_S = 900


def aws_session_due() -> bool:
    """Whether the session needs a fresh set soon: none held, or it ends within AWS_SESSION_DUE_S.
    A long run's heartbeat says so, and the backend answers with an invoke that carries one."""
    creds = AWS_SESSION
    return not creds or (creds["expires"] - datetime.now(timezone.utc)).total_seconds() < AWS_SESSION_DUE_S


class NoScopedCredentials(RuntimeError):
    """A shared runtime has no scoped set (none sent, or expired): its role must not stand in."""


def aws_session_kwargs() -> dict:
    """boto3 client kwargs for the scoped credentials. Without a live set: {} (the execution role)
    on a per-owner runtime, and NoScopedCredentials on a shared one -- fail closed, the call is
    refused rather than made on a role that would have to cover every tenant."""
    creds = AWS_SESSION
    if not creds or creds["expires"] <= datetime.now(timezone.utc):
        if SHARED:
            raise NoScopedCredentials("no scoped AWS credentials for this session")
        return {}
    access, secret, token = creds["keys"]
    return {"aws_access_key_id": access, "aws_secret_access_key": secret, "aws_session_token": token}
