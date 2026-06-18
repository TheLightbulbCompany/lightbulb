import io
import json
import os
import tarfile

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

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
