"""Route53 records for app hostnames, driven through botocore Stubber."""

import boto3
import pytest
from botocore.stub import Stubber

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


def _cname_change(host, target="cname.vercel-dns.com"):
    return {
        "HostedZoneId": ZONE,
        "ChangeBatch": {
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": host,
                        "Type": "CNAME",
                        "TTL": 300,
                        "ResourceRecords": [{"Value": target}],
                    },
                }
            ]
        },
    }


def _list_txt(stub, name, rrset):
    stub.add_response(
        "list_resource_record_sets",
        {"ResourceRecordSets": [rrset] if rrset else [], "IsTruncated": False, "MaxItems": "1"},
        {"HostedZoneId": ZONE, "StartRecordName": name, "StartRecordType": "TXT", "MaxItems": "1"},
    )


def _txt_set(name, *values):
    return {
        "Name": name + ".",
        "Type": "TXT",
        "TTL": 300,
        "ResourceRecords": [{"Value": v} for v in values],
    }


def _txt_batch(*changes):
    return {"HostedZoneId": ZONE, "ChangeBatch": {"Changes": list(changes)}}


def _create(name, *values):
    return {
        "Action": "CREATE",
        "ResourceRecordSet": {
            "Name": name,
            "Type": "TXT",
            "TTL": 300,
            "ResourceRecords": [{"Value": v} for v in values],
        },
    }


SHARED = "_vercel.test.example"
A = '"vc-domain-verify=acme.apps.test.example,h1"'
B = '"vc-domain-verify=other.apps.test.example,h2"'
ENTRY = {"type": "TXT", "domain": SHARED, "value": A.strip('"'), "reason": "x"}


async def test_point_creates_cname_and_txt(r53):
    r53.add_response("change_resource_record_sets", _ok(), _cname_change(f"acme.{DOMAIN}"))
    _list_txt(r53, SHARED, None)
    r53.add_response("change_resource_record_sets", _ok(), _txt_batch(_create(SHARED, A)))
    await mod.point(
        "acme",
        cname_target="cname.vercel-dns.com",
        verification=[ENTRY, {"type": "A", "domain": "ignored", "value": "1.2.3.4"}],
    )


async def test_point_merges_into_shared_txt(r53):
    r53.add_response("change_resource_record_sets", _ok(), _cname_change(f"acme.{DOMAIN}"))
    _list_txt(r53, SHARED, _txt_set(SHARED, B))
    r53.add_response(
        "change_resource_record_sets",
        _ok(),
        _txt_batch({"Action": "DELETE", "ResourceRecordSet": _txt_set(SHARED, B)}, _create(SHARED, B, A)),
    )
    await mod.point("acme", cname_target="cname.vercel-dns.com", verification=[ENTRY])


async def test_point_preview_targets_preview_host(r53):
    r53.add_response("change_resource_record_sets", _ok(), _cname_change(f"acme-preview.{DOMAIN}"))
    await mod.point("acme", cname_target="cname.vercel-dns.com", verification=[], preview=True)


async def test_point_retries_on_conflict(r53):
    r53.add_response("change_resource_record_sets", _ok(), _cname_change(f"acme.{DOMAIN}"))
    _list_txt(r53, SHARED, None)
    r53.add_client_error("change_resource_record_sets", "InvalidChangeBatch")
    _list_txt(r53, SHARED, _txt_set(SHARED, B))
    r53.add_response(
        "change_resource_record_sets",
        _ok(),
        _txt_batch({"Action": "DELETE", "ResourceRecordSet": _txt_set(SHARED, B)}, _create(SHARED, B, A)),
    )
    await mod.point("acme", cname_target="cname.vercel-dns.com", verification=[ENTRY])


@pytest.mark.parametrize("domain", ["test.example", "_vercel.evil.com", "acme.test.example"])
async def test_point_rejects_bad_txt_domain(r53, domain):
    with pytest.raises(ValueError):
        await mod.point(
            "acme",
            cname_target="t",
            verification=[{"type": "TXT", "domain": domain, "value": "v"}],
        )


def _remove_lists(stub, cname_rows, txt_by_name):
    host = f"acme.{DOMAIN}"
    for start, rows in [(host, cname_rows), (f"acme-preview.{DOMAIN}", [])]:
        stub.add_response(
            "list_resource_record_sets",
            {"ResourceRecordSets": rows, "IsTruncated": False, "MaxItems": "10"},
            {"HostedZoneId": ZONE, "StartRecordName": start, "MaxItems": "10"},
        )
    return host


async def test_remove_deletes_cname_and_only_its_txt_value(r53):
    host = f"acme.{DOMAIN}"
    cname = _rrset(host, "CNAME", "cname.vercel-dns.com")
    other = _rrset(f"acme2.{DOMAIN}", "CNAME", "z")
    _remove_lists(r53, [cname, other], None)
    r53.add_response(
        "change_resource_record_sets",
        _ok(),
        _txt_batch({"Action": "DELETE", "ResourceRecordSet": cname}),
    )
    # names: _vercel.<host>, _vercel.<preview>, _vercel.<APPS_DOMAIN>, _vercel.<root>
    _list_txt(r53, f"_vercel.{host}", None)
    _list_txt(r53, f"_vercel.acme-preview.{DOMAIN}", None)
    _list_txt(r53, f"_vercel.{DOMAIN}", None)
    _list_txt(r53, SHARED, _txt_set(SHARED, A, B))
    r53.add_response(
        "change_resource_record_sets",
        _ok(),
        _txt_batch({"Action": "DELETE", "ResourceRecordSet": _txt_set(SHARED, A, B)}, _create(SHARED, B)),
    )
    await mod.remove("acme")


async def test_remove_last_value_deletes_set(r53):
    host = f"acme.{DOMAIN}"
    _remove_lists(r53, [], None)
    _list_txt(r53, f"_vercel.{host}", _txt_set(f"_vercel.{host}", A))
    r53.add_response(
        "change_resource_record_sets",
        _ok(),
        _txt_batch({"Action": "DELETE", "ResourceRecordSet": _txt_set(f"_vercel.{host}", A)}),
    )
    _list_txt(r53, f"_vercel.acme-preview.{DOMAIN}", None)
    _list_txt(r53, f"_vercel.{DOMAIN}", None)
    _list_txt(r53, SHARED, _txt_set(SHARED, B))
    await mod.remove("acme")


@pytest.mark.parametrize("bad", ["A", "a", "-ab", "ab-", "a_b", "x" * 41, "Acme", "a.b", "acme-preview"])
async def test_invalid_name_rejected_before_route53(r53, bad):
    with pytest.raises(ValueError):
        await mod.point(bad, cname_target="t", verification=[])
    with pytest.raises(ValueError):
        await mod.remove(bad)
    with pytest.raises(ValueError):
        mod.check_name(bad)
