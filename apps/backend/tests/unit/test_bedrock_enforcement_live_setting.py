"""BEDROCK_ENFORCEMENT_LIVE is the single Metronome-cutover flag. It must
exist on the settings object, be a bool, and default to False so the legacy
DDB deduct stays the sole live biller (and the kill-switch stays disarmed)
until the explicit env-var flip. Both the connection_pool legacy-deduct gate
and bedrock_enforcement.enforce_on_low_balance read this SAME flag, so the
cutover is order-independent: ingest to Metronome is always-on shadow, and
flipping the flag swaps biller + arms enforcement atomically."""

from core.config import Settings


def test_bedrock_enforcement_live_defaults_false():
    settings = Settings()
    assert hasattr(settings, "BEDROCK_ENFORCEMENT_LIVE")
    assert settings.BEDROCK_ENFORCEMENT_LIVE is False


def test_bedrock_enforcement_live_reads_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_ENFORCEMENT_LIVE", "true")
    settings = Settings()
    assert settings.BEDROCK_ENFORCEMENT_LIVE is True
