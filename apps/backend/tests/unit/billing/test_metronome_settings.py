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
    # Phase 3 cutover: Metronome is the sole ledger; "ddb" is no longer valid.
    assert settings.LEDGER_BACKEND == "metronome"
