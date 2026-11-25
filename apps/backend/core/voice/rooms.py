"""Whether OpenAI Live voice is configured (Spark's voice sessions, routers/os.py)."""

from __future__ import annotations

from core.config import settings


def live_configured() -> bool:
    # CDK seeds the secret with a random non-sk- value; that counts as unset (voice stays dark).
    return settings.OPENAI_API_KEY.startswith("sk-")
