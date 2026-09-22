"""Lambda entry point for the lightbulb-{env}-provision state machine's RunProvisioner
state. Runs from the backend image (``python -m awslambdaric
scripts.provision_lambda.handler``) and calls the SAME ``provision_runner.run``.

The runner's exit ABI becomes the typed errors the machine's Retry/Catch match on:
transient -> ``ProvisionTransient`` (retried), deterministic -> ``ProvisionFailed``.
No waiting here beyond what ``run`` does; waits belong in machine states.

Lambda has no ECS ``secrets:`` injection, so each cold start first reads every secret
the backend task definition injects (``LAMBDA_SECRET_ENV_MAP``, ``{ENV_NAME: secret
arn}``, built by CDK from the same map) into the environment -- BEFORE anything
imports ``core.config``, whose Settings read env once at import.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys


def load_secrets(secret_map: dict[str, str], environ=os.environ, *, client=None) -> None:
    """Set each mapped env var from Secrets Manager. A var already set wins."""
    missing = {name: arn for name, arn in secret_map.items() if name not in environ}
    if not missing:
        return
    if client is None:
        import boto3

        client = boto3.client("secretsmanager")
    for name, arn in missing.items():
        environ[name] = client.get_secret_value(SecretId=arn)["SecretString"]


load_secrets(json.loads(os.environ.get("LAMBDA_SECRET_ENV_MAP") or "{}"))

from scripts import provision_runner  # noqa: E402 -- imports core.config; secrets must be loaded first


class ProvisionTransient(Exception):
    """The runner exited transient; the machine retries."""


class ProvisionFailed(Exception):
    """The runner exited deterministic; the machine records the failure and stops."""


# The runtime interface client may already have put a handler on root; either way INFO
# must reach CloudWatch, as the ECS runner's basicConfig did.
logging.basicConfig(stream=sys.stdout)
logging.getLogger().setLevel(logging.INFO)

# One loop for the container's life: a warm invocation must not find a module-level
# client bound to a loop a previous asyncio.run() closed.
_loop = asyncio.new_event_loop()


def handler(event, context):
    owner_id = event["owner_id"]
    code = _loop.run_until_complete(provision_runner.run(owner_id))
    if code == provision_runner.EXIT_TRANSIENT:
        raise ProvisionTransient(f"provision for {owner_id} failed transiently (see the row's last_error)")
    if code != provision_runner.EXIT_OK:
        raise ProvisionFailed(f"provision for {owner_id} failed deterministically (see the row's last_error)")
    return {"outcome": "provisioned"}
