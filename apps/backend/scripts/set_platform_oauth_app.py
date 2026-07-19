"""Seed (or rotate) a platform OAuth app client in Secrets Manager.

Writes ``isol8/<env>/integrations/platform-oauth/<name>`` — the Isol8-owned
OAuth client that platform-app providers (see ``OAuthSpec.platform_app``) use
for every owner's connect. Reusable ops tool, not a one-shot backfill: run it
again to rotate the secret after a Meta app-secret reset.

Usage (operator, with AWS SSO creds):

    cd apps/backend
    uv run python scripts/set_platform_oauth_app.py --env dev --name meta-instagram \
        --client-id 1049347507456011
    # → prompts for the client secret (never passed on argv / shell history)

The secret payload is ``{"client_id": ..., "client_secret": ...}``. The backend
caches reads for ~5 minutes (platform_oauth_app_store), so a rotation takes
effect within that window without a deploy.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

import boto3
from botocore.exceptions import ClientError


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", required=True, choices=["dev", "staging", "prod"])
    ap.add_argument("--name", required=True, help="platform app name, e.g. meta-instagram")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    client_secret = getpass.getpass("Client secret: ").strip()
    if not client_secret:
        print("empty client secret — aborting", file=sys.stderr)
        return 1

    secret_name = f"isol8/{args.env}/integrations/platform-oauth/{args.name}"
    payload = json.dumps({"client_id": args.client_id, "client_secret": client_secret})

    sm = boto3.client("secretsmanager", region_name=args.region)
    try:
        sm.create_secret(Name=secret_name, SecretString=payload)
        action = "created"
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceExistsException":
            raise
        sm.put_secret_value(SecretId=secret_name, SecretString=payload)
        action = "rotated"
    print(f"{action}: {secret_name} (client_id={args.client_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
