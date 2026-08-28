"""Re-point per-user ECS services at the current Cloud Map registry.

WHY THIS EXISTS
---------------
Per-user OpenClaw services are created by the BACKEND, not by CloudFormation, so
a CDK change that replaces the Cloud Map namespace cannot fix them: CFN does not
know they exist. They keep the OLD ``serviceRegistries[].registryArn``, which no
longer resolves once the old namespace is gone. A service in that state looks
fine in the console and fails at launch.

``UpdateService`` accepts ``serviceRegistries`` for ``deploymentController: ECS``
services -- which all of ours are -- so this is a configuration write, not a
rebuild. The provision path re-asserts the registry on every provision
(``ecs_manager._service_registries``), so this script only has to catch services
that will not be provisioned before their next launch.

WHAT IT WILL NOT DO
-------------------
It refuses to touch a service with ``desiredCount > 0``. Changing
``serviceRegistries`` on a running service triggers a new deployment, and this
fleet runs ``minimumHealthyPercent: 0`` (the EFS single-writer invariant), so ECS
stops the old task before starting the new one -- a hard gap, not a rolling one.
Running owners go through the admin UI reprovision instead, which carries the
drain guard. This script exists for the parked majority, where the write is free:
no task exists, and the new configuration takes effect at the next launch.

USAGE
-----
Dry run (default) then apply::

    cd apps/backend
    CLOUD_MAP_SERVICE_ARN=<new-arn> ECS_CLUSTER=<cluster> uv run python \
        scripts/migrate_cloud_map_registry.py
    ... --apply

Re-runnable and idempotent: a service already on the target ARN is skipped.
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3


def _services(ecs, cluster: str) -> list[str]:
    names: list[str] = []
    for page in ecs.get_paginator("list_services").paginate(cluster=cluster):
        names.extend(arn.rsplit("/", 1)[-1] for arn in page.get("serviceArns", []))
    return sorted(names)


def _describe(ecs, cluster: str, names: list[str]) -> list[dict]:
    out: list[dict] = []
    # DescribeServices caps at 10 per call.
    for i in range(0, len(names), 10):
        resp = ecs.describe_services(cluster=cluster, services=names[i : i + 10])
        out.extend(resp.get("services", []))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster", default=os.environ.get("ECS_CLUSTER", ""))
    ap.add_argument("--registry-arn", default=os.environ.get("CLOUD_MAP_SERVICE_ARN", ""))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--prefix", default="openclaw-", help="only touch services with this prefix")
    ap.add_argument("--apply", action="store_true", help="write; default is a read-only plan")
    args = ap.parse_args()

    if not args.cluster or not args.registry_arn:
        print("ERROR: --cluster and --registry-arn (or ECS_CLUSTER / CLOUD_MAP_SERVICE_ARN) required")
        return 2

    ecs = boto3.client("ecs", region_name=args.region)
    names = [n for n in _services(ecs, args.cluster) if n.startswith(args.prefix)]
    print(f"{len(names)} {args.prefix}* services in {args.cluster.rsplit('/', 1)[-1]}")
    print(f"target registry: {args.registry_arn}\n")

    stats = {"ok": 0, "moved": 0, "planned": 0, "running_skipped": 0, "error": 0}

    for svc in _describe(ecs, args.cluster, names):
        name = svc["serviceName"]
        current = [r.get("registryArn") for r in svc.get("serviceRegistries", [])]
        desired = svc.get("desiredCount", 0)

        if current == [args.registry_arn]:
            stats["ok"] += 1
            continue

        if desired > 0:
            # A registry change forces a new deployment, and minimumHealthyPercent:0
            # means the old task stops before the new one starts. Not this script's
            # call to make -- reprovision through the admin UI, which drains first.
            print(f"  SKIP    {name}  desiredCount={desired} — reprovision via the admin UI")
            stats["running_skipped"] += 1
            continue

        if not args.apply:
            print(f"  plan    {name}  {current or '[]'} -> target")
            stats["planned"] += 1
            continue

        try:
            ecs.update_service(
                cluster=args.cluster,
                service=name,
                serviceRegistries=[{"registryArn": args.registry_arn}],
            )
            print(f"  moved   {name}")
            stats["moved"] += 1
        except Exception as e:  # noqa: BLE001 — report and continue; one bad service must not stop the sweep
            print(f"  ERROR   {name}  {e}")
            stats["error"] += 1

    print("\n" + "  ".join(f"{k}={v}" for k, v in stats.items() if v))
    return 1 if stats["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
