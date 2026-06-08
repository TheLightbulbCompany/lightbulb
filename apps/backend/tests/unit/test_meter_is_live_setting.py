"""METER_IS_LIVE is the Plan-4-owned cutover flag. It must exist on the
settings object, be a bool, and default to False so the legacy deduct stays
the sole live biller until the explicit env-var flip. Plan 3's meter endpoint
reads this SAME flag to decide shadow-only (False) vs live deplete (True)."""

from core.config import Settings


def test_meter_is_live_defaults_false():
    settings = Settings()
    assert hasattr(settings, "METER_IS_LIVE")
    assert settings.METER_IS_LIVE is False


def test_meter_is_live_reads_env(monkeypatch):
    monkeypatch.setenv("METER_IS_LIVE", "true")
    settings = Settings()
    assert settings.METER_IS_LIVE is True
