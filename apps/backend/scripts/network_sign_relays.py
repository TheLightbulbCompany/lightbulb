"""Sign Lightbulb Connect's relays.json (the engine's src/relay.rs verifies it).

    uv run python -m scripts.network_sign_relays --generate
    uv run python -m scripts.network_sign_relays --sign dev|prod

``--generate`` makes the two minisign signing keys straight into Secrets Manager
(``lightbulb/prod/network/relays_signing_key_{1,2}``; it refuses if either exists) and
prints only their PUBLIC strings, which go into the engine's ``relay::KEYS``.
``--sign <env>`` reads key 1 and writes ``relays.json`` and ``relays.json.minisig`` under
``apps/frontend/public/network-relays/<env>/``, expiring in 90 days. Re-sign before then
(docs/runbooks/network-relay.md). Needs AWS credentials; the secret never leaves memory.

Pure-Python minisign: a public key is ``base64("Ed" + key_id[8] + pk[32])``; a signature
is the prehashed ``"ED"`` kind (Ed25519 over BLAKE2b-512 of the file), the only kind the
engine accepts. A secret is stored as ``base64(key_id[8] + ed25519_seed[32])``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SECRET_NAMES = tuple(f"lightbulb/prod/network/relays_signing_key_{i}" for i in (1, 2))
RELAYS = {
    "prod": "https://relay.thelightbulb.company",
    "dev": "https://relay-dev.thelightbulb.company",
}
LIFETIME_S = 90 * 86_400
OUT_DIR = Path(__file__).resolve().parents[2] / "frontend" / "public" / "network-relays"


def _raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def new_secret() -> tuple[str, str]:
    """(secret string, minisign public string) for a fresh key."""
    key_id, seed = os.urandom(8), os.urandom(32)
    public = base64.b64encode(b"Ed" + key_id + _raw_public(Ed25519PrivateKey.from_private_bytes(seed)))
    return base64.b64encode(key_id + seed).decode(), public.decode()


def sign(secret: str, body: bytes, now: int) -> str:
    """The minisign signature file for ``body``."""
    raw = base64.b64decode(secret)
    key_id, key = raw[:8], Ed25519PrivateKey.from_private_bytes(raw[8:])
    sig = key.sign(hashlib.blake2b(body, digest_size=64).digest())
    trusted = f"timestamp:{now}\tfile:relays.json\thashed"
    global_sig = key.sign(sig + trusted.encode())
    return (
        "untrusted comment: Lightbulb Connect relays.json\n"
        f"{base64.b64encode(b'ED' + key_id + sig).decode()}\n"
        f"trusted comment: {trusted}\n"
        f"{base64.b64encode(global_sig).decode()}\n"
    )


def relays_body(env: str, now: int) -> bytes:
    return (json.dumps({"v": 1, "relays": [RELAYS[env]], "expires_at": now + LIFETIME_S}) + "\n").encode()


def generate(sm) -> list[str]:
    """Creates both keys in Secrets Manager; returns their public strings."""
    for name in SECRET_NAMES:
        try:
            sm.get_secret_value(SecretId=name)
        except sm.exceptions.ResourceNotFoundException:
            continue
        raise SystemExit(f"{name} already exists; refusing to replace a signing key")
    publics = []
    for name in SECRET_NAMES:
        secret, public = new_secret()
        sm.create_secret(Name=name, SecretString=secret, Description="Lightbulb Connect relays.json signing key")
        publics.append(public)
    return publics


def sign_env(sm, env: str, out_dir: Path = OUT_DIR, now: int | None = None) -> Path:
    now = int(time.time()) if now is None else now
    secret = sm.get_secret_value(SecretId=SECRET_NAMES[0])["SecretString"]
    body = relays_body(env, now)
    target = out_dir / env
    target.mkdir(parents=True, exist_ok=True)
    (target / "relays.json").write_bytes(body)
    (target / "relays.json.minisig").write_text(sign(secret, body, now))
    return target


def main(argv: list[str] | None = None, sm=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--generate", action="store_true")
    group.add_argument("--sign", choices=sorted(RELAYS))
    args = parser.parse_args(argv)
    if sm is None:
        import boto3

        sm = boto3.client("secretsmanager", region_name="us-east-1")
    if args.generate:
        for i, public in enumerate(generate(sm), 1):
            print(f"relays signing key {i} public: {public}")
    else:
        print(f"wrote {sign_env(sm, args.sign)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
