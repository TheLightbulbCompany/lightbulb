"""The projector Lambda re-declares the live-event registry; keep the two equal.

A type the projector does not list is silently dropped from the LIVE# outbox,
so drift here is a delivery outage with no error anywhere.
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
