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
    # Metronome's global USD-(cents) credit-type id — identical in our
    # sandbox and Metronome's own docs example, so it ships as a constant
    # default rather than per-env wiring.
    assert settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID == "2714e483-4ff1-48e4-9e25-ac732e8f24f2"
    # Contracts are per-customer objects persisted on the billing row — an
    # env-wide contract id setting must not creep back in.
    assert not hasattr(settings, "METRONOME_CREDITS_CONTRACT_ID")
    # Default ledger backend MUST still be ddb in Phase 2 (cutover is Phase 3).
    assert settings.LEDGER_BACKEND == "ddb"
