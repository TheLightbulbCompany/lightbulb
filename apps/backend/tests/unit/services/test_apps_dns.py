"""Route53 records for app hostnames, driven through botocore Stubber."""

import boto3
import pytest
from botocore.stub import ANY, Stubber

from core.config import settings
from core.services import apps_dns as mod

ZONE = "Z123"
DOMAIN = "apps.test.example"


@pytest.fixture
def r53(monkeypatch):
    monkeypatch.setattr(settings, "APPS_DOMAIN", DOMAIN)
    monkeypatch.setattr(settings, "APPS_HOSTED_ZONE_ID", ZONE)
    client = boto3.client("route53", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(mod, "_client", lambda: client)
    with Stubber(client) as stub:
        yield stub
        stub.assert_no_pending_responses()


def _ok():
    return {"ChangeInfo": {"Id": "/change/C1", "Status": "PENDING", "SubmittedAt": "2026-01-01"}}


def _rrset(name, typ, value):
    return {"Name": name + ".", "Type": typ, "TTL": 300, "ResourceRecords": [{"Value": value}]}


def test_hostname_uses_setting(monkeypatch):
    monkeypatch.setattr(settings, "APPS_DOMAIN", DOMAIN)
    assert mod.hostname("acme") == f"acme.{DOMAIN}"
    assert mod.preview_hostname("acme") == f"acme-preview.{DOMAIN}"


async def test_point_upserts_cname_and_txt(r53):
    r53.add_response(
        "change_resource_record_sets",
        _ok(),
        {
            "HostedZoneId": ZONE,
            "ChangeBatch": {
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": f"acme.{DOMAIN}",
                            "Type": "CNAME",
                            "TTL": 300,
                            "ResourceRecords": [{"Value": "cname.vercel-dns.com"}],
                        },
                    },
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": f"_vercel.{DOMAIN}",
                            "Type": "TXT",
                            "TTL": 300,
                            "ResourceRecords": [{"Value": '"vc-domain-verify=abc"'}],
                        },
                    },
                ]
            },
        },
    )
    await mod.point(
        "acme",
        cname_target="cname.vercel-dns.com",
        verification=[
            {"type": "TXT", "domain": f"_vercel.{DOMAIN}", "value": "vc-domain-verify=abc", "reason": "x"},
            {"type": "A", "domain": "ignored", "value": "1.2.3.4", "reason": "x"},
        ],
    )


async def test_remove_deletes_both(r53):
    host = f"acme.{DOMAIN}"
    cname = _rrset(host, "CNAME", "cname.vercel-dns.com")
    txt = _rrset(f"_vercel.{host}", "TXT", '"v"')
    other = _rrset(f"acme2.{DOMAIN}", "CNAME", "z")
    # lists: host, _vercel.host, preview host, _vercel.preview host
    for start, rows in [
        (host, [cname, other]),
        (f"_vercel.{host}", [txt]),
        (f"acme-preview.{DOMAIN}", []),
        (f"_vercel.acme-preview.{DOMAIN}", []),
    ]:
        r53.add_response(
            "list_resource_record_sets",
            {"ResourceRecordSets": rows, "IsTruncated": False, "MaxItems": "10"},
            {"HostedZoneId": ZONE, "StartRecordName": start, "MaxItems": "10"},
        )
    r53.add_response(
        "change_resource_record_sets",
        _ok(),
        {
            "HostedZoneId": ZONE,
            "ChangeBatch": {
                "Changes": [
                    {"Action": "DELETE", "ResourceRecordSet": cname},
                    {"Action": "DELETE", "ResourceRecordSet": txt},
                ]
            },
        },
    )
    await mod.remove("acme")


async def test_remove_nothing_to_delete_is_noop(r53):
    for _ in range(4):
        r53.add_response(
            "list_resource_record_sets",
            {"ResourceRecordSets": [], "IsTruncated": False, "MaxItems": "10"},
            {"HostedZoneId": ZONE, "StartRecordName": ANY, "MaxItems": "10"},
        )
    await mod.remove("acme")


@pytest.mark.parametrize("bad", ["A", "a", "-ab", "ab-", "a_b", "x" * 41, "Acme", "a.b"])
async def test_invalid_name_rejected_before_route53(r53, bad):
    with pytest.raises(ValueError):
        await mod.point(bad, cname_target="t", verification=[])
    with pytest.raises(ValueError):
        await mod.remove(bad)
