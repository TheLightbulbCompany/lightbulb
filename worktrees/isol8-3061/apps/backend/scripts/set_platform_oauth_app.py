"""Seed (or rotate) a platform OAuth app client in Secrets Manager.

Writes ``lightbulb/<env>/integrations/platform-oauth/<name>`` — the platform-owned
OAuth client that platform-app providers (see ``OAuthSpec.platform_app``) use
for every owner's connect. Reusable ops tool, not a one-shot backfill: run it
again to rotate the secret after a Meta app-secret reset.

Usage (operator, with AWS SSO creds):

    cd apps/backend
    uv run python scripts/set_platform_oauth_app.py --env dev --name meta-instagram \
        --client-id 1049347507456011
    # → prompts for the client secret (never passed on argv / shell history)

    uv run python scripts/set_platform_oauth_app.py --env dev --name slack \
        --client-id abc123 --with-signing-secret
    # → prompts for both client secret and signing secret

The secret payload is ``{"client_id": ..., "client_secret": ..., "signing_secret": ...}``. The backend
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
    # Operators run this with SSO creds under the isol8-admin profile (see the
    # other ops scripts). A bare boto3 client would use the DEFAULT chain instead
    # and silently target the wrong account (or fail with no creds), so pin the
    # profile explicitly. Pass --profile "" to use ambient credentials (CI role).
    ap.add_argument("--profile", default="isol8-admin")
    ap.add_argument(
        "--with-signing-secret",
        action="store_true",
        help="also prompt for a signing secret (Slack signs webhooks with one; Meta does not)",
    )
    args = ap.parse_args()

    client_secret = getpass.getpass("Client secret: ").strip()
    if not client_secret:
        print("empty client secret — aborting", file=sys.stderr)
        return 1

    secret_name = f"lightbulb/{args.env}/integrations/platform-oauth/{args.name}"
    payload_dict = {"client_id": args.client_id, "client_secret": client_secret}
    if args.with_signing_secret:
        signing_secret = getpass.getpass("Signing secret: ").strip()
        if not signing_secret:
            print("empty signing secret — aborting", file=sys.stderr)
            return 1
        payload_dict["signing_secret"] = signing_secret
    payload = json.dumps(payload_dict)

    session = boto3.Session(profile_name=args.profile or None, region_name=args.region)
    # Surface the resolved account up front: writing a platform secret to the
    # wrong account is silent (a bare create_secret succeeds anywhere), so make
    # "which account did this land in" visible rather than a guess.
    account = session.client("sts").get_caller_identity()["Account"]
    sm = session.client("secretsmanager")
    try:
        sm.create_secret(Name=secret_name, SecretString=payload)
        action = "created"
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceExistsException":
            raise
        sm.put_secret_value(SecretId=secret_name, SecretString=payload)
        action = "rotated"
    print(f"{action}: {secret_name} (client_id={args.client_id}) in account {account} [{args.region}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
