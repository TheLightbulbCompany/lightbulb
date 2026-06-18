import hashlib
import io
import json
import os
import tarfile
from unittest.mock import MagicMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import core.config as core_config  # noqa: E402
import scripts.backfill_marketplace_requirements as backfill  # noqa: E402
from core.services import marketplace_service as mp_service  # noqa: E402
from scripts.backfill_marketplace_requirements import scan_tarball  # noqa: E402


def _make_tarball(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_scan_tarball_detects_requirements():
    tb = _make_tarball(
        {
            "./openclaw.json": json.dumps({"identity": {"name": "T"}}),
            "./pipelines/daily-feed.lobster": (
                "--tool slack\n--tool llm-task\ncurl https://production.plaid.com/x $PLAID_SECRET\n"
            ),
        }
    )
    fragment = scan_tarball(tb)
    keys = {r["key"]: r["disposition"] for r in fragment["requirements"]}
    assert keys["plaid"] == "byo"
    assert keys["slack"] == "connect"
    assert keys["llm-task"] == "included"
    assert fragment["scan_incomplete"] is False
    # requires.credentials carries only non-channel connect rows: slack is a
    # channel (gated via the Channels tab), plaid is byo, llm-task is included
    # — so none feed the credential hire gate here.
    assert fragment["requires"]["credentials"] == []


def test_scan_tarball_empty_tree_is_empty():
    tb = _make_tarball({"./openclaw.json": "{}"})
    fragment = scan_tarball(tb)
    assert fragment["requirements"] == []
    assert fragment["requires"]["credentials"] == []


def _s3_stub(tar_bytes: bytes, manifest_obj: dict):
    """A boto3-S3-like stub: serves the tarball + manifest.json and records puts."""
    puts: dict[str, bytes] = {}

    def get_object(Bucket, Key):  # noqa: N803 — boto3 kwarg casing
        if Key.endswith("workspace.tar.gz"):
            body = tar_bytes
        elif Key.endswith("manifest.json"):
            body = json.dumps(manifest_obj).encode("utf-8")
        else:
            raise KeyError(Key)
        return {"Body": io.BytesIO(body)}

    def put_object(Bucket, Key, Body, **_kw):  # noqa: N803
        puts[Key] = Body

    s3 = MagicMock()
    s3.get_object.side_effect = get_object
    s3.put_object.side_effect = put_object
    return s3, puts


def test_main_apply_writes_requirements_to_both_stores():
    """main()'s WRITE path (S3 manifest.json + DDB manifest_json + recomputed
    manifest_sha256) — the exact untested surface that masked the original
    fails-open bug. Verifies requirements land where the storefront + hire gate
    read them, and that sha256 matches the written manifest bytes."""
    tar = _make_tarball(
        {
            "./openclaw.json": json.dumps({"identity": {"name": "T"}}),
            "./pipelines/p.lobster": "steps:\n  - command: gog.gmail.search --query x\n",
        }
    )
    # Rich S3 manifest carries safety_flags that must be preserved.
    s3_manifest = {"name": "T", "file_count": 3, "safety_flags": [], "agent_id": "a1"}
    s3, puts = _s3_stub(tar, s3_manifest)
    row = {
        "listing_id": "l1",
        "version": 1,
        "status": "published",
        "s3_prefix": "listings/l1/v1/",
        "manifest_json": {"name": "T", "version": 1, "agent_id": "a1"},
    }
    table = MagicMock()

    with (
        patch.object(backfill, "_published_listings", return_value=[row]),
        patch("boto3.client", return_value=s3),
        patch.object(core_config.settings, "MARKETPLACE_ARTIFACTS_BUCKET", "bkt"),
        patch.object(mp_service, "_listings_table", return_value=table),
        patch("sys.argv", ["backfill", "--apply"]),
    ):
        backfill.main()

    # S3 manifest.json rewritten WITH requirements, safety_flags preserved.
    written = json.loads(puts["listings/l1/v1/manifest.json"])
    assert any(r["key"] == "gog" for r in written["requirements"])
    assert "safety_flags" in written  # rich field not clobbered
    # sha256 matches the exact bytes written to S3.
    expected_sha = hashlib.sha256(json.dumps(written, sort_keys=True).encode("utf-8")).hexdigest()

    # DDB row updated: manifest_json carries requirements + the matching sha.
    table.update_item.assert_called_once()
    kw = table.update_item.call_args.kwargs
    assert kw["Key"] == {"listing_id": "l1", "version": 1}
    vals = kw["ExpressionAttributeValues"]
    assert any(r["key"] == "gog" for r in vals[":mj"]["requirements"])
    assert vals[":sha"] == expected_sha


def test_main_skips_row_when_manifest_unreadable():
    """A transient/credentials error reading manifest.json must SKIP the row,
    never rewrite the authoritative manifest from a degraded source."""
    tar = _make_tarball({"./openclaw.json": "{}"})
    s3 = MagicMock()

    def get_object(Bucket, Key):  # noqa: N803
        if Key.endswith("workspace.tar.gz"):
            return {"Body": io.BytesIO(tar)}
        raise RuntimeError("AccessDenied")  # manifest.json read fails

    s3.get_object.side_effect = get_object
    row = {"listing_id": "l1", "version": 1, "status": "published", "s3_prefix": "listings/l1/v1/"}
    table = MagicMock()

    with (
        patch.object(backfill, "_published_listings", return_value=[row]),
        patch("boto3.client", return_value=s3),
        patch.object(core_config.settings, "MARKETPLACE_ARTIFACTS_BUCKET", "bkt"),
        patch.object(mp_service, "_listings_table", return_value=table),
        patch("sys.argv", ["backfill", "--apply"]),
    ):
        backfill.main()

    s3.put_object.assert_not_called()  # never rewrite from a degraded source
    table.update_item.assert_not_called()
