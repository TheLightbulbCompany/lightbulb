"""Phone startup preferences; legacy documents stay untouched on read."""

VOICES = ("marin", "quartz", "willow")
DEFAULT_VOICE = "marin"
LEGACY_VOICE = "Warm"


def phone_voice(value: object) -> str:
    return value if isinstance(value, str) and value in VOICES else DEFAULT_VOICE
