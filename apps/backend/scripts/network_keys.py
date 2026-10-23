"""Lightbulb Connect issuer key ceremony (owner-run, once).

Generates the three Ed25519 issuer seeds, writes each ONLY to Secrets Manager
(``lightbulb/{env}/network/issuer_key_{kid}``, base64), and prints only the public halves:
the Rust lines for the engine's ``ISSUER_KEYS`` (``src/proof.rs``), each with its base64, and
the hex lines for the backend's ``network_issuer.ISSUER_PUBLIC_KEYS``. It never overwrites a
key: a kid whose secret already exists (an earlier, partial run) is skipped and reported, and
only the missing ones are created; that kid's public half is the one the earlier run printed.

    AWS_PROFILE=isol8-admin uv run python -m scripts.network_keys
"""

from __future__ import annotations

import base64
import sys

import boto3
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEYS = (("prod", "p1"), ("prod", "p2"), ("dev", "dev"))


def secret_name(env: str, kid: str) -> str:
    return f"lightbulb/{env}/network/issuer_key_{kid}"


def main(sm=None) -> int:
    sm = sm or boto3.client("secretsmanager", region_name="us-east-1")
    rust, hexes = [], []
    for env, kid in KEYS:
        name = secret_name(env, kid)
        try:
            sm.describe_secret(SecretId=name)
            print(f"{name} already exists; skipped (never overwritten).", file=sys.stderr)
            continue
        except sm.exceptions.ResourceNotFoundException:
            pass
        private = Ed25519PrivateKey.generate()
        # create_secret itself refuses an existing name (ResourceExistsException).
        sm.create_secret(Name=name, SecretString=base64.b64encode(private.private_bytes_raw()).decode())
        public = private.public_key().public_bytes_raw()
        rust.append(f'    ("{kid}", [{", ".join(map(str, public))}]), // {base64.b64encode(public).decode()}')
        hexes.append(f'    "{kid}": bytes.fromhex("{public.hex()}"),')
    if rust:
        print("// lightbulb-connect src/proof.rs ISSUER_KEYS")
        print("\n".join(rust))
        print("# apps/backend/core/services/network_issuer.py ISSUER_PUBLIC_KEYS")
        print("\n".join(hexes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
