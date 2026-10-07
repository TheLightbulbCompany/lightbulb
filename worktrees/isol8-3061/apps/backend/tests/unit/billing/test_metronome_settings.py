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
    # Rate card is created per-env in the Metronome console; wired via env.
    assert settings.METRONOME_RATE_CARD_ID == ""
    # Metronome's global USD-(cents) credit-type id — identical in our
    # sandbox and Metronome's own docs example, so it ships as a constant
    # default rather than per-env wiring.
    assert settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID == "2714e483-4ff1-48e4-9e25-ac732e8f24f2"
    # Contracts are per-customer objects persisted on the billing row — an
    # env-wide contract id setting must not creep back in.
    assert not hasattr(settings, "METRONOME_CREDITS_CONTRACT_ID")
    # The DDB ledger is deleted — none of its settings may creep back in.
    assert not hasattr(settings, "LEDGER_BACKEND")
    assert not hasattr(settings, "CREDITS_TABLE")
    assert not hasattr(settings, "CREDIT_TRANSACTIONS_TABLE")
    assert not hasattr(settings, "BEDROCK_ENFORCEMENT_LIVE")
    # The gate's lazy-refresh window (see core/services/metronome_gate.py).
    assert settings.METRONOME_GATE_STALENESS_S == 60
