"""Meeting transcripts: one text object per occurrence at ``<owner>/voice/transcripts/<room>/<id>.txt`` in the
owner-state bucket. The key is built from the caller's owner id, so a record never reaches another owner's object.
Sync: call via ``run_in_thread``."""

from __future__ import annotations

import uuid

from core.services import hermes_profiles

#: The largest transcript kept (about 80 hours of talk); a bigger one is refused.
MAX_BYTES = 5 * 1024 * 1024


def _key(owner_id: str, room_id: str, transcript_id: str) -> str:
    return f"{owner_id}/voice/transcripts/{uuid.UUID(room_id)}/{uuid.UUID(transcript_id)}.txt"


def put(owner_id: str, room_id: str, data: bytes) -> str:
    """Write one transcript and return its id."""
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError(f"a transcript must be 1 to {MAX_BYTES} bytes")
    transcript_id = str(uuid.uuid4())
    hermes_profiles.put_owner_object(_key(owner_id, room_id, transcript_id), data)
    return transcript_id


def get(owner_id: str, room_id: str, transcript_id: str) -> bytes | None:
    """The transcript, or None when there is none (gone, or an id that names no transcript). A failed read raises."""
    try:
        key = _key(owner_id, room_id, transcript_id)
    except ValueError:
        return None
    return hermes_profiles.get_owner_object(key)


def size(owner_id: str, room_id: str, transcript_id: str) -> int | None:
    """The transcript's byte size by a HEAD, or None when there is none. A failed read raises."""
    try:
        key = _key(owner_id, room_id, transcript_id)
    except ValueError:
        return None
    return hermes_profiles.owner_object_size(key)
