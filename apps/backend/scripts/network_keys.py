"""Lightbulb Connect issuer key ceremony (owner-run, once).

Generates the three Ed25519 issuer seeds, writes each ONLY to Secrets Manager
(``lightbulb/{env}/network/issuer_key_{kid}``, base64), and prints only the public halves:
the Rust lines for the engine's ``ISSUER_KEYS`` (``src/proof.rs``), each with its base64, and
the hex lines for the backend's ``network_issuer.ISSUER_PUBLIC_KEYS``. It never overwrites a
key: a kid whose secret already exists (an earlier, partial run) is reported and kept, its
seed read back (read-only) only to print its public half; the missing ones are created. So a
rerun always prints all three kids, in both formats.

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
            seed = sm.get_secret_value(SecretId=name)["SecretString"]
            print(f"{name} already exists; kept (never overwritten).", file=sys.stderr)
            private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed))
        except sm.exceptions.ResourceNotFoundException:
            private = Ed25519PrivateKey.generate()
            # create_secret itself refuses an existing name (ResourceExistsException).
            sm.create_secret(Name=name, SecretString=base64.b64encode(private.private_bytes_raw()).decode())
        public = private.public_key().public_bytes_raw()
        rust.append(f'    ("{kid}", [{", ".join(map(str, public))}]), // {base64.b64encode(public).decode()}')
        hexes.append(f'    "{kid}": bytes.fromhex("{public.hex()}"),')
    print("// lightbulb-connect src/proof.rs ISSUER_KEYS")
    print("\n".join(rust))
    print("# apps/backend/core/services/network_issuer.py ISSUER_PUBLIC_KEYS")
    print("\n".join(hexes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
