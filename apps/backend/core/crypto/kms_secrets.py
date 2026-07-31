"""Thin wrapper around AWS KMS for encrypting container secrets at rest.

Purpose: protect long-lived per-container secrets (Ed25519 operator device
seeds, gateway tokens) so that reading the `containers` DynamoDB table alone
is not sufficient to impersonate a container — the reader also needs
`kms:Decrypt` on the backend's CMK.

Why not envelope encryption with data keys? The payloads we protect are all
32 bytes or shorter (Ed25519 seeds are 32 bytes; gateway tokens are ~64
chars). Well under the KMS direct-encrypt limit (4 KiB), so we use
`kms:Encrypt` / `kms:Decrypt` directly instead of wrapping a data key.

Why not Secrets Manager? One secret per container at $0.40/month becomes
prohibitive once we have thousands of pods, and a single shared secret with
per-container nested fields loses the per-key audit trail. Direct KMS +
DynamoDB is cheaper, auditable via CloudTrail, and gives us the same
"plaintext never lives on disk" guarantee.

Why not use a single KMS-held Ed25519 keypair on the backend side? KMS does
not support Ed25519 signing (only RSA/ECDSA as of 2026). We need Ed25519 to
match OpenClaw's device-auth protocol, so signing happens in-process — KMS
just protects the key at rest.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

import boto3

from core.config import settings

logger = logging.getLogger(__name__)

_kms_client = None


def _client():
    """Lazy boto3 KMS client — deferred so import-time works without creds."""
    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms", region_name=settings.AWS_REGION)
    return _kms_client


def _key_id() -> str:
    """Resolve the configured CMK ARN/alias.

    Raises if `CONTAINER_SECRETS_KMS_KEY_ID` isn't set, so misconfiguration
    fails fast at provision time rather than at connect time.
    """
    key_id = settings.CONTAINER_SECRETS_KMS_KEY_ID
    if not key_id:
        raise RuntimeError("CONTAINER_SECRETS_KMS_KEY_ID is not configured — cannot encrypt container secrets")
    return key_id


def encrypt_bytes(plaintext: bytes, *, encryption_context: Optional[dict[str, str]] = None) -> str:
    """Encrypt `plaintext` with the container-secrets CMK.

    Returns the ciphertext as a base64-encoded string so it can be stored
    directly in a DynamoDB `S` attribute. The raw KMS ciphertext is binary
    and `S` attributes are strings.

    `encryption_context` is an optional dict bound to the ciphertext — the
    decrypt call must pass the same context or KMS refuses. We use this to
    bind the ciphertext to an `owner_id` so a stolen encrypted-key row can't
    be replayed against a different container.
    """
    kwargs: dict = {
        "KeyId": _key_id(),
        "Plaintext": plaintext,
    }
    if encryption_context:
        kwargs["EncryptionContext"] = encryption_context
    resp = _client().encrypt(**kwargs)
    return base64.b64encode(resp["CiphertextBlob"]).decode("ascii")


def decrypt_bytes(ciphertext_b64: str, *, encryption_context: Optional[dict[str, str]] = None) -> bytes:
    """Inverse of `encrypt_bytes`. KMS verifies `encryption_context` matches."""
    ciphertext_blob = base64.b64decode(ciphertext_b64)
    kwargs: dict = {"CiphertextBlob": ciphertext_blob}
    if encryption_context:
        kwargs["EncryptionContext"] = encryption_context
    resp = _client().decrypt(**kwargs)
    return resp["Plaintext"]


def encrypt_string(plaintext: str, *, encryption_context: Optional[dict[str, str]] = None) -> str:
    """Convenience wrapper for string payloads (e.g. gateway tokens)."""
    return encrypt_bytes(plaintext.encode("utf-8"), encryption_context=encryption_context)
