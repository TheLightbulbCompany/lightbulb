"""Meeting transcripts: one text object per occurrence at ``<owner>/voice/transcripts/<room>/<id>.txt`` in the
owner-state bucket. The key is built from the caller's owner id, so a record never reaches another owner's object.
Sync: call via ``run_in_thread``."""

from __future__ import annotations

import uuid

from core.services import hermes_profiles

#: The largest transcript kept (about 80 hours of talk); a bigger one is refused.
MAX_BYTES = 5 * 1024 * 1024


def _key(owner_id: str, room_id: str, transcript_id: str) -> str | None:
    """None for an id that names no transcript (not a UUID)."""
    try:
        return f"{owner_id}/voice/transcripts/{uuid.UUID(room_id)}/{uuid.UUID(transcript_id)}.txt"
    except ValueError:
        return None


def put(owner_id: str, room_id: str, data: bytes, *, name: str) -> str:
    """Write one transcript and return its id. The id is derived from ``name``, so a retried write of the same
    transcript replaces its object instead of adding one."""
    transcript_id = str(uuid.uuid5(uuid.NAMESPACE_URL, name))
    key = _key(owner_id, room_id, transcript_id)
    if not key or not 0 < len(data) <= MAX_BYTES:
        raise ValueError(f"a transcript must be 1 to {MAX_BYTES} bytes, in a room")
    hermes_profiles.put_owner_object(key, data)
    return transcript_id


def get(owner_id: str, room_id: str, transcript_id: str) -> bytes | None:
    """The transcript, or None when there is none. A failed read raises."""
    key = _key(owner_id, room_id, transcript_id)
    return hermes_profiles.get_owner_object(key) if key else None


def size(owner_id: str, room_id: str, transcript_id: str) -> int | None:
    """The transcript's byte size by a HEAD, or None when there is none. A failed read raises."""
    key = _key(owner_id, room_id, transcript_id)
    return hermes_profiles.owner_object_size(key) if key else None
