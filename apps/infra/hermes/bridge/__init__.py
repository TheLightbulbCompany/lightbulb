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
MODEL = os.environ.get("ANTHROPIC_MODEL", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")


def default_s3_prefix(owner: str) -> str:
    """Owner first. The execution role grants `<owner>/*` on the replicas bucket, so a
    prefix beginning with a literal `hermes/` is outside the grant — the old default was
    backwards and only the runtime env override kept it from AccessDenying."""
    return f"{owner}/hermes"


S3_PREFIX = os.environ.get("S3_PREFIX", default_s3_prefix(OWNER)).strip("/")
STARTED = time.time()


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
    if _boot_secrets.get("bedrockBearer"):
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = _boot_secrets["bedrockBearer"]
