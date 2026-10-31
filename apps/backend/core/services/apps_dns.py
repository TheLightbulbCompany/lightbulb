"""Route53 records for app hostnames: {name}.{APPS_DOMAIN} (and the preview host).

Sync boto3 run in a thread; change batches are submitted, never waited on.
"""

import asyncio
import re

import boto3
from botocore.exceptions import ClientError

from core.config import settings

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
_TTL = 300


def _client():
    return boto3.client("route53")


def check_name(name: str) -> str:
    if not _NAME.match(name) or name.endswith("-preview"):
        raise ValueError(f"invalid app name: {name!r}")
    return name


def hostname(name: str) -> str:
    return f"{name}.{settings.APPS_DOMAIN}"


def preview_hostname(name: str) -> str:
    return f"{name}-preview.{settings.APPS_DOMAIN}"


def _root() -> str:
    return ".".join(settings.APPS_DOMAIN.split(".")[-2:])


def _zone() -> str:
    return settings.APPS_HOSTED_ZONE_ID


def _txt_target(domain: str) -> str:
    d = domain.rstrip(".")
    if not d.startswith("_vercel.") or not d.endswith("." + _root()):
        raise ValueError(f"refusing verification TXT at {domain!r}")
    return d


def _owner(value: str) -> str:
    """Host a vc-domain-verify value belongs to: '"vc-domain-verify=<host>,<hash>"'."""
    return value.strip('"').split("=", 1)[-1].split(",")[0]


def _merge_txt(c, name: str, add: list[str], drop_hosts: tuple[str, ...] = ()) -> None:
    """Read-modify-write one shared TXT set. The DELETE of the set exactly as read
    makes the batch a compare-and-swap: a concurrent writer fails it with
    InvalidChangeBatch and we re-read."""
    for attempt in range(3):
        resp = c.list_resource_record_sets(
            HostedZoneId=_zone(), StartRecordName=name, StartRecordType="TXT", MaxItems="1"
        )
        cur = next(
            (r for r in resp["ResourceRecordSets"] if r["Name"].rstrip(".") == name and r["Type"] == "TXT"),
            None,
        )
        old = [r["Value"] for r in cur["ResourceRecords"]] if cur else []
        new = [v for v in old if _owner(v) not in drop_hosts]
        new += [v for v in add if v not in new]
        if cur and new == old:
            return
        if not cur and not new:
            return
        changes = []
        if cur:
            changes.append({"Action": "DELETE", "ResourceRecordSet": cur})
        if new:
            changes.append(
                {
                    "Action": "CREATE",
                    "ResourceRecordSet": {
                        "Name": name,
                        "Type": "TXT",
                        "TTL": cur["TTL"] if cur else _TTL,
                        "ResourceRecords": [{"Value": v} for v in new],
                    },
                }
            )
        try:
            c.change_resource_record_sets(HostedZoneId=_zone(), ChangeBatch={"Changes": changes})
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidChangeBatch" or attempt == 2:
                raise


def _point(host: str, cname_target: str, txts: dict[str, list[str]]) -> None:
    c = _client()
    c.change_resource_record_sets(
        HostedZoneId=_zone(),
        ChangeBatch={
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": host,
                        "Type": "CNAME",
                        "TTL": _TTL,
                        "ResourceRecords": [{"Value": cname_target}],
                    },
                }
            ]
        },
    )
    for name, values in txts.items():
        _merge_txt(c, name, values)


def _remove(name: str) -> None:
    c = _client()
    hosts = (hostname(name), preview_hostname(name))
    changes = []
    for host in hosts:
        resp = c.list_resource_record_sets(HostedZoneId=_zone(), StartRecordName=host, MaxItems="10")
        changes += [
            {"Action": "DELETE", "ResourceRecordSet": r}
            for r in resp["ResourceRecordSets"]
            if r["Name"].rstrip(".") == host and r["Type"] == "CNAME"
        ]
    if changes:
        c.change_resource_record_sets(HostedZoneId=_zone(), ChangeBatch={"Changes": changes})
    names = dict.fromkeys([f"_vercel.{h}" for h in hosts] + [f"_vercel.{settings.APPS_DOMAIN}", f"_vercel.{_root()}"])
    for n in names:
        _merge_txt(c, n, [], hosts)


async def point(name: str, *, cname_target: str, verification: list[dict], preview: bool = False) -> None:
    check_name(name)
    txts: dict[str, list[str]] = {}
    for v in verification:
        if v.get("type") == "TXT":
            txts.setdefault(_txt_target(v["domain"]), []).append('"' + v["value"] + '"')
    host = preview_hostname(name) if preview else hostname(name)
    await asyncio.to_thread(_point, host, cname_target, txts)


async def remove(name: str) -> None:
    check_name(name)
    await asyncio.to_thread(_remove, name)
