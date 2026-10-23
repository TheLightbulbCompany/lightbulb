"""Lightbulb Connect issuer key ceremony (owner-run, once).

Generates the three Ed25519 issuer seeds, writes each ONLY to Secrets Manager
(``lightbulb/{env}/network/issuer_key_{kid}``, base64), and prints only the public halves:
the Rust lines for the engine's ``ISSUER_KEYS`` (``src/proof.rs``), each with its base64.
Refuses to run when any of the three secrets already exists, so it never overwrites a key.

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
    for env, kid in KEYS:
        try:
            sm.describe_secret(SecretId=secret_name(env, kid))
        except sm.exceptions.ResourceNotFoundException:
            continue
        print(f"{secret_name(env, kid)} already exists; refusing to overwrite any key.", file=sys.stderr)
        return 1
    lines = []
    for env, kid in KEYS:
        private = Ed25519PrivateKey.generate()
        seed = base64.b64encode(private.private_bytes_raw()).decode()
        # create_secret itself refuses an existing name (ResourceExistsException).
        sm.create_secret(Name=secret_name(env, kid), SecretString=seed)
        public = private.public_key().public_bytes_raw()
        lines.append(f'    ("{kid}", [{", ".join(map(str, public))}]), // {base64.b64encode(public).decode()}')
    print("pub const ISSUER_KEYS: [(&str, [u8; 32]); 3] = [")
    print("\n".join(lines))
    print("];")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
