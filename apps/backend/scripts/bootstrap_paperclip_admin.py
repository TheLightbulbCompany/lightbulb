"""Bootstrap the Paperclip instance admin (``ADMIN_EMAIL`` below).

Runs ONCE per environment via ``aws ecs execute-command`` against the
backend task — the script lives in the backend container image at
``/app/scripts/bootstrap_paperclip_admin.py``.

What it does
------------

1. Reads ``ENVIRONMENT`` + ``PAPERCLIP_INTERNAL_URL`` from container env.
2. Generates a 32-byte random password for ``ADMIN_EMAIL``.
3. Calls Paperclip's Better Auth ``/sign-up`` endpoint to create the
   admin user (a regular non-admin user at this point).
4. Writes ``{email, password}`` to AWS Secrets Manager at
   ``isol8/{env}/paperclip_admin_credentials``.
5. Prints a runbook hint: operator must complete the board-claim flow
   to grant the new user ``instance_admin`` role (Paperclip security
   feature — promotion is gated by an interactive URL, not an API call).

How to run
----------

::

    aws ecs run-task \\
      --profile isol8-admin --region us-east-1 \\
      --cluster isol8-{env}-container-... \\
      --task-definition isol8-{env}-paperclip-bootstrap-admin \\
      --launch-type FARGATE \\
      --network-configuration 'awsvpcConfiguration={subnets=[<private-subnet>],securityGroups=[<paperclip-task-sg>],assignPublicIp=DISABLED}'

Idempotency
-----------

If the secret already exists, the script aborts unless ``--force`` is
passed. With ``--force`` it overwrites the secret AND attempts to sign
up admin again — Better Auth will reject with "user already exists",
in which case the operator should reset Paperclip's user table or use
a different admin email.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys

# Make `core.*` importable when this script is invoked directly via
# python /app/scripts/bootstrap_paperclip_admin.py (the canonical
# invocation when an operator runs it via `aws ecs execute-command`
# inside a backend task container). Adding the backend root to
# sys.path is the simplest fix; alternatives (PYTHONPATH env var,
# or `python -m scripts.bootstrap_paperclip_admin`) are operator-
# error-prone.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import boto3  # noqa: E402
import httpx  # noqa: E402

from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Plus-addressed alias of admin@isol8.co. Routes to the same inbox via
# Gmail/Workspace plus-addressing, but Paperclip's Better Auth treats
# it as a distinct user identity. Why distinct: admin@isol8.co is also
# a real Isol8 end-user account (signs in via Clerk, has a Clerk org +
# OpenClaw container). If the bootstrap admin and the end-user shared
# the same email, every provision_org call for that org would collide
# with this signup ("user already exists"), and the end-user's normal
# /teams session would inherit the bootstrap admin's instance_admin
# god-mode role across all tenants. Splitting the email keeps the
# privileged service identity isolated from the regular user identity
# while still letting one human read both inboxes.
#
# Dev predates this change and was bootstrapped with the bare
# admin@isol8.co email — safe there only because no dev user has ever
# signed up as admin@isol8.co. Don't replicate the dev value in new
# environments.
ADMIN_EMAIL = "admin+paperclip@isol8.co"
SECRET_NAME_TEMPLATE = "isol8/{env}/paperclip_admin_credentials"


def _public_host(env: str) -> str:
    """Public host the operator opens in their browser to complete the
    board-claim flow. Per the URL rename in #497, dev is
    ``dev.company.isol8.co`` and prod is ``company.isol8.co``.
    """
    if env == "prod":
        return "company.isol8.co"
    return f"{env}.company.isol8.co"


async def _signup(paperclip_url: str, password: str) -> dict:
    async with httpx.AsyncClient(base_url=paperclip_url, timeout=30.0) as http:
        client = PaperclipAdminClient(http_client=http)
        return await client.sign_up_user(
            email=ADMIN_EMAIL,
            password=password,
            name="Isol8 Backend Admin",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing admin secret and attempt re-signup. Use only after manual reset.",
    )
    args = parser.parse_args()

    env = os.environ.get("ENVIRONMENT", "")
    if not env:
        sys.exit("ENVIRONMENT env var required (set by ECS task definition)")
    paperclip_url = os.environ.get("PAPERCLIP_INTERNAL_URL", "")
    if not paperclip_url:
        sys.exit("PAPERCLIP_INTERNAL_URL env var required (set by ECS task definition)")

    secret_name = SECRET_NAME_TEMPLATE.format(env=env)
    secrets_client = boto3.client("secretsmanager")

    # Secret is pre-created by CDK (auth-stack.ts) with a placeholder
    # value, so GetSecretValue should always succeed. Detect "already
    # populated" by checking the payload shape.
    try:
        existing = secrets_client.get_secret_value(SecretId=secret_name)
    except secrets_client.exceptions.ResourceNotFoundException:
        sys.exit(
            f"Secret {secret_name} doesn't exist. CDK deploy should have created "
            "a placeholder — check that auth-stack.ts is current and the dev "
            "deploy succeeded."
        )

    try:
        existing_payload = json.loads(existing["SecretString"])
        already_populated = "email" in existing_payload and "password" in existing_payload
    except (json.JSONDecodeError, KeyError, TypeError):
        already_populated = False  # placeholder string, not JSON

    if already_populated and not args.force:
        logger.info(
            "Admin secret %s is already populated. Re-run with --force to "
            "overwrite (only after manually deleting the Better Auth user, "
            "since signup will fail with 'user already exists').",
            secret_name,
        )
        return

    password = secrets.token_urlsafe(32)
    try:
        result = asyncio.run(_signup(paperclip_url, password))
    except PaperclipApiError as e:
        sys.exit(f"Paperclip signup failed: {e.status_code} {e.body}")
    except Exception as e:  # noqa: BLE001 - top-level guard
        sys.exit(f"Paperclip signup failed: {type(e).__name__}: {e}")

    new_user_id = (result.get("user") or {}).get("id")
    logger.info("Signed up %s in Paperclip (paperclip_user_id=%s)", ADMIN_EMAIL, new_user_id)

    payload = json.dumps({"email": ADMIN_EMAIL, "password": password})
    secrets_client.put_secret_value(SecretId=secret_name, SecretString=payload)
    logger.info("Wrote credentials to %s", secret_name)

    host = _public_host(env)
    print()
    print("=" * 78)
    print("BOOTSTRAP STEP 1 OF 2 COMPLETE")
    print("=" * 78)
    print(f"  - Paperclip user '{ADMIN_EMAIL}' created (paperclip_user_id={new_user_id})")
    print(f"  - Credentials written to Secrets Manager: {secret_name}")
    print()
    print("BOOTSTRAP STEP 2 OF 2 - OPERATOR ACTION REQUIRED:")
    print("=" * 78)
    print("  Paperclip's instance_admin role is granted via the board-claim flow")
    print("  (paperclip/server/src/board-claim.ts). API-only promotion isn't")
    print("  supported - it's an intentional security boundary.")
    print()
    print(f"  1. Port-forward to Paperclip directly (the proxy at https://{host}/")
    print(f"     would sign you in as your Clerk identity, not {ADMIN_EMAIL}):")
    print()
    print("       aws ssm start-session --profile isol8-admin --region us-east-1 \\")
    print("         --target <paperclip-task-id> \\")
    print("         --document-name AWS-StartPortForwardingSession \\")
    print('         --parameters \'portNumber=["3100"],localPortNumber=["3100"]\'')
    print()
    print("  2. Open http://localhost:3100 - look for the board-claim banner.")
    print("     If absent: tail Paperclip's CloudWatch logs for the claim URL")
    print("     (logged at startup as 'Board claim available at: ...').")
    print()
    print(f"  3. Sign in with email={ADMIN_EMAIL}, password from Secrets Manager.")
    print(f"  4. Click 'Claim board ownership'. {ADMIN_EMAIL} now has instance_admin.")
    print()
    print("After step 2, the backend's provisioning flow can act as admin and")
    print("create per-user companies. No backend restart needed - admin session")
    print("is acquired lazily on first provisioning call.")
    print("=" * 78)


if __name__ == "__main__":
    main()
