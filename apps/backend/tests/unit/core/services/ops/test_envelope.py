from core.services.ops import envelope as env


def test_envelope_shape_ok():
    e = env.envelope({"running": 3}, freshness_ts="2026-06-30T00:00:00Z")
    assert e["data"] == {"running": 3}
    assert e["source_state"] == "ok"
    assert e["errors"] == []
    assert e["freshness_ts"] == "2026-06-30T00:00:00Z"
