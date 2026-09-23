"""Lambda entry point for the lightbulb-{env}-provision state machine's RunProvisioner
state. Runs from the backend image (``python -m awslambdaric
scripts.provision_lambda.handler``) and calls the SAME ``provision_runner.run``.

The runner's exit ABI becomes the typed errors the machine's Retry/Catch match on:
transient -> ``ProvisionTransient`` (retried), deterministic -> ``ProvisionFailed``.
No waiting here beyond what ``run`` does; waits belong in machine states.

Lambda has no ECS ``secrets:`` injection and a 4 KB env limit the backend's env
exceeds, so the first invocation rebuilds the task definition's environment --
the plain env from an SSM parameter and every injected secret from Secrets
Manager, both written by CDK from the task def's own maps -- BEFORE anything
imports ``core.config``, whose Settings read env once at import.

That rebuild and the backend import run in the first INVOKE, not at module init:
Lambda caps init at 10 s, and the first cold start after a deploy (a fresh image
to page in, ~20 Secrets Manager reads, the whole backend to import) hit it on dev
2026-09-23 -- Lambda then re-ran init inside the invoke, paying it twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys


def load_environment(environ=os.environ, *, ssm=None, secretsmanager=None) -> None:
    """Rebuild the backend task definition's environment. A var already set wins.

    The plain env (``LAMBDA_ENV_PARAMETER``, an SSM parameter holding the task def's env
    as JSON -- it is larger than Lambda's 4 KB env limit) first, then each secret in
    ``LAMBDA_SECRET_ENV_MAP`` (``{ENV_NAME: secret arn}``) from Secrets Manager."""
    import boto3

    parameter = environ.get("LAMBDA_ENV_PARAMETER")
    if parameter:
        ssm = ssm or boto3.client("ssm")
        plain = json.loads(ssm.get_parameter(Name=parameter)["Parameter"]["Value"])
        for name, value in plain.items():
            environ.setdefault(name, value)
    secret_map = json.loads(environ.get("LAMBDA_SECRET_ENV_MAP") or "{}")
    for name, arn in secret_map.items():
        if name not in environ:
            secretsmanager = secretsmanager or boto3.client("secretsmanager")
            environ[name] = secretsmanager.get_secret_value(SecretId=arn)["SecretString"]


#: ``scripts.provision_runner``, imported by the first invocation once the env is loaded.
provision_runner = None


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
    global provision_runner
    if provision_runner is None:
        load_environment()
        from scripts import provision_runner as runner  # imports core.config: env first

        provision_runner = runner
    owner_id = event["owner_id"]
    code = _loop.run_until_complete(provision_runner.run(owner_id))
    if code == provision_runner.EXIT_TRANSIENT:
        raise ProvisionTransient(f"provision for {owner_id} failed transiently (see the row's last_error)")
    if code != provision_runner.EXIT_OK:
        raise ProvisionFailed(f"provision for {owner_id} failed deterministically (see the row's last_error)")
    return {"outcome": "provisioned"}
