"""METER_IS_LIVE controls whether the AWS Bedrock invocation-log meter
depletes the ledger. It must exist on the settings object, be a bool, and
default True — the AWS meter is the sole biller (the legacy connection_pool
deduct was deleted in Phase 3). Plan 3's meter endpoint reads this SAME flag to
decide shadow-only (False) vs live deplete (True)."""

from core.config import Settings


def test_meter_is_live_defaults_true():
    settings = Settings()
    assert hasattr(settings, "METER_IS_LIVE")
    # Phase 3 cutover: the AWS invocation-log meter is the sole biller, so the
    # meter depletes by default. The legacy connection_pool deduct is deleted.
    assert settings.METER_IS_LIVE is True


def test_meter_is_live_reads_env(monkeypatch):
    monkeypatch.setenv("METER_IS_LIVE", "false")
    settings = Settings()
    assert settings.METER_IS_LIVE is False
