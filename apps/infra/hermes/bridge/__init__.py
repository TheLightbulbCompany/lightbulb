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
