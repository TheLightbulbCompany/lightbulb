"""Per-workspace AgentCore Runtime naming, trust and boot secret.

One runtime per workspace. That gives each workspace its own execution role, which is AWS's own
recommended multi-tenant control — the docs are explicit that AgentCore does not enforce that a
sessionId belongs to the calling user, and name per-tenant IAM principals as the fix.

**The role is the per-owner Bedrock role, re-trusted, not a new one.**
``lightbulb-{env}-owner-bedrock-{owner}`` already carries what a runtime needs: the owner's
Bedrock application-inference profile, the owner-state S3 prefix and AppSync publish. Minting a
second role would duplicate all of it and drift.

The request/environment builders live in ``hermes_provisioning``
(``build_hermes_runtime_request`` / ``build_hermes_runtime_env``) — this module holds only what
is true of any runtime regardless of the image inside it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re

from core.config import settings
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)

AGENTCORE_SERVICE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"

#: AgentCore runtime names take letters, digits and underscores, and must start with a letter.
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]")
#: Keep clear of the service's name ceiling while staying unique per owner.
_MAX_NAME_LEN = 48


def _env() -> str:
    # m5: an unset ENVIRONMENT (empty string is Settings' own default) must never silently
    # produce a double-slash name/secret path (e.g. "lightbulb//hermes/<owner>/boot") -- that
    # falls outside every environment-scoped IAM grant. Matches the fallback the original brief
    # snippet already had for boot_secret_name; centralized here since runtime_name_for shares
    # this same helper.
    return settings.ENVIRONMENT or "dev"


def runtime_name_for(owner_id: str) -> str:
    """A stable, AWS-legal runtime name for this workspace.

    Owner ids carry characters AgentCore rejects and can exceed the name ceiling, so the name
    is a sanitised prefix plus a hash of the FULL id. The hash is what keeps two owners whose
    ids share a sanitised prefix from colliding onto one runtime — which would put two
    workspaces on one EFS mount.
    """
    digest = hashlib.sha256(owner_id.encode()).hexdigest()[:12]
    stem = _NAME_SAFE.sub("_", owner_id)
    prefix = f"lb_{_env()}_"
    room = _MAX_NAME_LEN - len(prefix) - len(digest) - 1
    return f"{prefix}{stem[: max(room, 0)]}_{digest}"


def trust_policy_with_agentcore(existing: dict | None, account_id: str) -> dict:
    """Add the AgentCore service principal to an owner role's trust policy, idempotently.

    Additive on purpose: during the migration a workspace may still be served by its ECS task,
    and removing that statement while the task is live would break it mid-turn. The ECS
    statement is dropped separately, after the fleet has moved.
    """
    doc = json.loads(json.dumps(existing)) if existing else {"Version": "2012-10-17", "Statement": []}
    statements = doc.setdefault("Statement", [])
    if not isinstance(statements, list):
        statements = [statements]
        doc["Statement"] = statements
    for statement in statements:
        principal_block = statement.get("Principal")
        # A bare-string Principal ("*" or an ARN) is valid IAM JSON; it names no service.
        principal = principal_block.get("Service") if isinstance(principal_block, dict) else None
        services = principal if isinstance(principal, list) else [principal]
        if AGENTCORE_SERVICE_PRINCIPAL in services:
            return doc  # already trusted; do not append a duplicate
    statements.append(
        {
            "Effect": "Allow",
            "Principal": {"Service": AGENTCORE_SERVICE_PRINCIPAL},
            "Action": "sts:AssumeRole",
            "Condition": {
                # Same confused-deputy fence the ECS statement uses: our account, and only
                # AgentCore resources within it.
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{settings.AWS_REGION}:{account_id}:*"},
            },
        }
    )
    return doc


async def ensure_agentcore_trust(owner_id: str, role_name: str, account_id: str) -> bool:
    """Make the owner's role assumable by AgentCore. Returns True when it changed anything."""
    import boto3

    iam = boto3.client("iam")
    got = await run_in_thread(iam.get_role, RoleName=role_name)
    current = got["Role"].get("AssumeRolePolicyDocument")
    updated = trust_policy_with_agentcore(current, account_id)
    if updated == current:
        return False
    await run_in_thread(iam.update_assume_role_policy, RoleName=role_name, PolicyDocument=json.dumps(updated))
    logger.info("agentcore trust added owner=%s role=%s", owner_id, role_name)
    return True


#: How long AgentCore keeps an idle session's microVM warm before reaping it. Short (owner ruling
#: 2026-10-09, was 4 h): a person typing wakes the runtime ahead of the send (routers/messaging
#: typing prewake), so a parked session costs no visible cold start, and an idle microVM is spend.
#: A running turn or follow-up reports HealthyBusy on /ping, which AgentCore never counts as idle.
#: 15 min (2026-10-27 cost review): ~95% of the AgentCore bill was idle warm memory, mostly this
#: tail after heartbeat wakes. 5 min (Phase 2, 8 Nov 2026): Workers run in their own task sessions,
#: stopped at the seal, so this tail is only the Router's between live uses; nothing prewakes on
#: typing any more. Reaches a live runtime only on its next roll (lifecycle drift).
IDLE_TIMEOUT_S = 5 * 60
#: AWS's hard ceiling for a microVM session; the backend rolls sessions at a quiet point before it.
MAX_LIFETIME_S = 8 * 60 * 60


BOOT_SECRET_ENV_VAR = "LIGHTBULB_SECRET_ID"


def boot_secret_name(owner_id: str) -> str:
    """The name of this owner's one JSON boot secret."""
    return f"lightbulb/{_env()}/hermes/{owner_id}/boot"


def derive_vault_token(owner_id: str, row_id: str) -> str:
    """The website-login vault's credential: the only thing the internal ``website-logins``
    routes accept (``routers/website_logins.py``). It exists because the agent's shell holds the
    hook token; this one lives only in the bridge's memory, delivered as a header on every
    backend invoke (``runtime_transport._vault_header``). Never in the boot secret: the execution
    role can read that, and the shell can read the role over the metadata service.

    Derived, not stored, the same way ``derive_gate_bearer`` is: HMAC-SHA256 under
    ``GATE_BEARER_HMAC_KEY`` with its own domain label, over the owner and the containers row's
    ``id`` -- so concurrent prepares write identical bytes, a teardown (new row id) rotates it,
    and nothing stores the plaintext. Rotating the HMAC key changes every owner's token; the
    next invoke delivers the new one.
    """
    key = settings.GATE_BEARER_HMAC_KEY
    if not key:
        raise RuntimeError("GATE_BEARER_HMAC_KEY is not configured; refusing to derive a vault token")
    if not row_id:
        raise ValueError(f"no containers row id for {owner_id}; refusing to derive a vault token")
    msg = f"lightbulb-vault-token:v1:{owner_id}:{row_id}".encode()
    digest = hmac.new(key.encode("utf-8"), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _read_secret(sm, name: str) -> dict | None:
    """The boot secret's JSON; None when absent, or scheduled for deletion
    (``create_or_update_secret`` retries that)."""
    try:
        return json.loads(sm.get_secret_value(SecretId=name)["SecretString"])
    except (sm.exceptions.ResourceNotFoundException, sm.exceptions.InvalidRequestException):
        return None


async def network_member_key(owner_id: str) -> str | None:
    """The workspace's PUBLIC member key: the engine's key spelling (lowercase base32, no padding,
    of the Ed25519 public key whose seed is ``networkKey`` -- the sidecar's ``identity.key``), so
    the author of every entry the cloud member signed. None when there is no boot secret or key."""
    import boto3
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)

    def _read() -> str | None:
        seed = (_read_secret(sm, boot_secret_name(owner_id)) or {}).get("networkKey")
        if not seed:
            return None
        public = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed)).public_key().public_bytes_raw()
        return base64.b32encode(public).decode().lower().rstrip("=")

    return await run_in_thread(_read)


async def write_boot_secret(
    owner_id: str,
    *,
    hook_token: str,
    gateway_token: str,
    bedrock_bearer: str,
) -> bool:
    """Converge the owner's boot secret (named ``boot_secret_name(owner_id)``, the value a
    runtime's ``BOOT_SECRET_ENV_VAR`` carries). Plaintext lives in exactly one place: this secret.

    Returns whether a BOOT-TIME input changed. A warm session reads the secret once, at cold
    start, so a True answer is the caller's cue to restart the idle session. Identical bytes are
    not rewritten, so concurrent runs converge without churning secret versions.

    Only the credentials are compared: an older secret still carrying a retired key
    (``providerChoice``/``providerSwitchFrozen``, or ``vaultToken`` -- which the execution role
    could read) is rewritten without it, and without that reading as a boot change.

    ``networkKey`` is the workspace's member key on the ``lightbulb`` network (read by the
    network sidecar, and by no backend module but this one; ``network_member_key`` derives
    its public half). It is carried over from the prior
    secret and minted only when absent, so a reconcile never rotates it; a mint is a boot change."""
    import boto3

    from core.secrets_manager import create_or_update_secret

    name = boot_secret_name(owner_id)
    payload = {
        "hookToken": hook_token,
        "gatewayToken": gateway_token,
        "bedrockBearer": bedrock_bearer,
    }
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)

    def _converge() -> bool:
        prior = _read_secret(sm, name)
        # ponytail: two concurrent FIRST mints race and the last write wins; both report a
        # boot change, so the restarted session reads the winner. A CAS needs a version check.
        payload["networkKey"] = (prior or {}).get("networkKey") or base64.b64encode(os.urandom(32)).decode()
        if prior == payload:
            return False
        create_or_update_secret(sm, name, json.dumps(payload))

        return {k: (prior or {}).get(k) for k in payload} != payload

    return await run_in_thread(_converge)
