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
    ``LAMBDA_SECRET_ENV_MAP`` (``{ENV_NAME: ECS secret valueFrom}``) from Secrets Manager.
    ECS's optional JSON key/version suffix is interpreted here, not sent as a SecretId."""
    import boto3

    parameter = environ.get("LAMBDA_ENV_PARAMETER")
    if parameter:
        ssm = ssm or boto3.client("ssm")
        plain = json.loads(ssm.get_parameter(Name=parameter)["Parameter"]["Value"])
        for name, value in plain.items():
            environ.setdefault(name, value)
    secret_map = json.loads(environ.get("LAMBDA_SECRET_ENV_MAP") or "{}")
    fetched: dict[tuple[str, str, str], str] = {}
    for name, arn in secret_map.items():
        if name not in environ:
            secretsmanager = secretsmanager or boto3.client("secretsmanager")
            parts = arn.split(":")
            secret_id, key, stage, version = arn, "", "", ""
            if len(parts) > 7 and parts[0] == "arn" and parts[2] == "secretsmanager":
                if len(parts) != 10:
                    raise ValueError("invalid ECS secret selector")
                secret_id, key, stage, version = ":".join(parts[:7]), *parts[7:]
                if stage and version:
                    raise ValueError("ECS secret selector cannot specify both version stage and ID")
            ref = (secret_id, stage, version)
            if ref not in fetched:
                request = {"SecretId": secret_id}
                if stage:
                    request["VersionStage"] = stage
                if version:
                    request["VersionId"] = version
                fetched[ref] = secretsmanager.get_secret_value(**request)["SecretString"]
            value = json.loads(fetched[ref])[key] if key else fetched[ref]
            if not isinstance(value, str):
                raise ValueError("ECS secret environment value must be a string")
            environ[name] = value


#: ``scripts.provision_runner``, imported by the first invocation once the env is loaded.
provision_runner = None
#: ``hermes_provisioning``, imported alongside it (reconcile mode).
hermes_provisioning = None

# ponytail: fits the 5-min Lambda timeout; a busy session returns updated-session-busy and
# takes the spec on its next start.
RECONCILE_IDLE_WAIT_S = 180.0


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
    global provision_runner, hermes_provisioning
    if provision_runner is None:
        try:
            load_environment()
        except Exception as exc:  # an SSM/Secrets blip: retried by the machine, not a failed provision
            raise ProvisionTransient(f"environment load failed: {type(exc).__name__}") from exc
        from core.services import hermes_provisioning as hp  # imports core.config: env first
        from scripts import provision_runner as runner

        provision_runner = runner
        hermes_provisioning = hp
    owner_id = event["owner_id"]
    mode = event.get("mode", "provision")
    if mode == "reconcile":
        hermes_provisioning.ROLL_IDLE_WAIT_S = RECONCILE_IDLE_WAIT_S
        result = _loop.run_until_complete(hermes_provisioning.reconcile_runtime(owner_id))
        if result == "failed":
            # Retried by the machine; after the retries it ends at ReconcileGaveUp, never at the
            # row writes (a reconcile must not stamp error onto a row a provision owns).
            raise ProvisionTransient(f"reconcile for {owner_id} failed (see provision.reconcile.failed)")
        return {"outcome": result}
    if mode != "provision":
        raise ProvisionFailed(f"unknown mode {mode!r}")
    code = _loop.run_until_complete(provision_runner.run(owner_id))
    if code == provision_runner.EXIT_TRANSIENT:
        raise ProvisionTransient(f"provision for {owner_id} failed transiently (see the row's last_error)")
    if code != provision_runner.EXIT_OK:
        raise ProvisionFailed(f"provision for {owner_id} failed deterministically (see the row's last_error)")
    return {"outcome": "provisioned"}
