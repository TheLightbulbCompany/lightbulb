"""Route53 records for app hostnames: {name}.{APPS_DOMAIN} (and the preview host).

Sync boto3 run in a thread; change batches are submitted, never waited on.
"""

import asyncio
import re

import boto3

from core.config import settings

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
_TTL = 300


def _client():
    return boto3.client("route53")


def _check(name: str) -> None:
    if not _NAME.match(name):
        raise ValueError(f"invalid app name: {name!r}")


def hostname(name: str) -> str:
    return f"{name}.{settings.APPS_DOMAIN}"


def preview_hostname(name: str) -> str:
    return f"{name}-preview.{settings.APPS_DOMAIN}"


def _point(name: str, cname_target: str, verification: list[dict]) -> None:
    changes = [
        {
            "Action": "UPSERT",
            "ResourceRecordSet": {
                "Name": hostname(name),
                "Type": "CNAME",
                "TTL": _TTL,
                "ResourceRecords": [{"Value": cname_target}],
            },
        }
    ]
    for v in verification:
        if v.get("type") == "TXT":
            changes.append(
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": v["domain"],
                        "Type": "TXT",
                        "TTL": _TTL,
                        "ResourceRecords": [{"Value": '"' + v["value"] + '"'}],
                    },
                }
            )
    _client().change_resource_record_sets(HostedZoneId=settings.APPS_HOSTED_ZONE_ID, ChangeBatch={"Changes": changes})


def _remove(name: str) -> None:
    c = _client()
    changes = []
    for host in (hostname(name), preview_hostname(name)):
        # The verification TXT sits at _vercel.<host>; list both names.
        for fqdn in (host, f"_vercel.{host}"):
            resp = c.list_resource_record_sets(
                HostedZoneId=settings.APPS_HOSTED_ZONE_ID, StartRecordName=fqdn, MaxItems="10"
            )
            changes += [
                {"Action": "DELETE", "ResourceRecordSet": r}
                for r in resp["ResourceRecordSets"]
                if r["Name"].rstrip(".") == fqdn and r["Type"] in ("CNAME", "TXT")
            ]
    if changes:
        c.change_resource_record_sets(HostedZoneId=settings.APPS_HOSTED_ZONE_ID, ChangeBatch={"Changes": changes})


async def point(name: str, *, cname_target: str, verification: list[dict]) -> None:
    _check(name)
    await asyncio.to_thread(_point, name, cname_target, verification)


async def remove(name: str) -> None:
    _check(name)
    await asyncio.to_thread(_remove, name)
