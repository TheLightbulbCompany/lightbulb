"""A cold restore emits one CloudWatch EMF line (stdout, raw JSON — the logger's "[bridge] "
prefix would make it plain text) so restore time is a metric, not a per-runtime log grep."""

import json

import pytest

from bridge import sync
from tests.test_sync_recency import FakeS3


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "lightbulb-prod-openclaw-replicas")
    fake = FakeS3({f"{sync.S3_PREFIX}/a1/{n}.md": ("x", 1.0) for n in range(3)})
    monkeypatch.setattr(sync, "_s3", lambda: fake)
    sync._restored.discard("a1")
    return fake


def test_a_cold_restore_emits_one_emf_line(home, s3, capsys):
    sync.restore_profile("a1")
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    assert len(lines) == 1
    doc = json.loads(lines[0])
    directive = doc["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "LightbulbAI"
    assert directive["Dimensions"] == [["env", "service"]]
    assert {m["Name"] for m in directive["Metrics"]} == {
        "bridge.restore.seconds", "bridge.restore.objects", "bridge.restore.disk_free_mb",
    }
    assert doc["env"] == "prod" and doc["service"] == "hermes-bridge"
    assert doc["bridge.restore.objects"] == 3 and doc["bridge.restore.disk_free_mb"] > 0


def test_a_warm_call_emits_nothing(home, s3, capsys):
    sync.restore_profile("a1")
    capsys.readouterr()
    sync.restore_profile("a1")
    assert not [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
