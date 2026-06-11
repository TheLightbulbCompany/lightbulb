"""BEDROCK_ENFORCEMENT_LIVE arms the revoke-only enforcement. It must exist
on the settings object, be a bool, and default to True: Metronome is the
only billing ledger, so a depleted balance must revoke the owner's Bedrock
IAM grant — there is no other biller to fall back to. An env-var "false"
puts a single env back into shadow (flag set, no revoke) during validation."""

from core.config import Settings


def test_bedrock_enforcement_live_defaults_true():
    settings = Settings()
    assert hasattr(settings, "BEDROCK_ENFORCEMENT_LIVE")
    assert settings.BEDROCK_ENFORCEMENT_LIVE is True


def test_bedrock_enforcement_live_reads_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_ENFORCEMENT_LIVE", "false")
    settings = Settings()
    assert settings.BEDROCK_ENFORCEMENT_LIVE is False
