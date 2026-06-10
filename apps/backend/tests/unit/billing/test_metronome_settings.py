"""The Metronome adapter settings exist with sane Phase-2 defaults."""

from core.config import settings


def test_metronome_settings_present_with_defaults():
    # Sandbox is the default base for dev/Phase-2 (Metronome is token-scoped:
    # same base URL, different token; a sandbox token only sees sandbox data).
    assert settings.METRONOME_BASE_URL == "https://api.metronome.com/v1"
    # Secrets default empty so the backend boots in non-Metronome contexts
    # (unit tests, the DDB-default prod path) without configuration.
    assert settings.METRONOME_API_KEY == ""
    assert settings.METRONOME_WEBHOOK_SECRET == ""
    assert settings.METRONOME_FAST_GATE_TABLE == ""
    # The lifecycle master switch defaults OFF: provisioning/funding/display
    # no-op until the per-env runbook (tokens + rate card + webhook secret)
    # flips it via service-stack env.
    assert settings.METRONOME_LIFECYCLE_ENABLED is False
    # Rate card is created per-env in the Metronome console; wired via env.
    assert settings.METRONOME_RATE_CARD_ID == ""
    # Contracts are per-customer objects persisted on the billing row — an
    # env-wide contract id setting must not creep back in.
    assert not hasattr(settings, "METRONOME_CREDITS_CONTRACT_ID")
    # Default ledger backend MUST still be ddb in Phase 2 (cutover is Phase 3).
    assert settings.LEDGER_BACKEND == "ddb"
