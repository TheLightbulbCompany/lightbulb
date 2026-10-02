"""The projector Lambda re-declares the live-event registry; keep the two equal.

Drift makes the projector REJECT the record rather than drop it quietly:
_validate_live_event raises on an unlisted type, the record is retried, then
parked in the failure bucket and paged (messaging-projector-failures-stored).
Today only room.access.removed / room.subscription.rotated reach this path --
_remove_member_with_notices is the one LIVE# writer -- so this equality is a
guard against future drift, not a fix for a live bug.
"""

import importlib.util
from pathlib import Path

from core.services.messaging import LIVE_EVENT_TYPES

_INDEX = Path(__file__).resolve().parents[4] / "infra" / "lambda" / "messaging-projector" / "index.py"


def test_projector_registry_matches_backend():
    spec = importlib.util.spec_from_file_location("messaging_projector_index", _INDEX)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.LIVE_EVENT_TYPES == LIVE_EVENT_TYPES
    assert "room.call.changed" not in LIVE_EVENT_TYPES  # the /calls orb's marker; Live replaced it
