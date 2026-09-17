"""Cross-process conformance test for the backend-owned-files contract (F3, M6-R36).

Before this fix, three lanes disagreed on which files are backend-owned and must never be
overwritten by outside content: ``hermes_profiles``'s own writer-raise set omitted
``lightbulb-facts.json``, ``catalog_service``'s marketplace-deploy skip list omitted
``SOUL.md``/``.revision``/the facts file entirely, and the bridge's own re-pull list (a
separate literal -- it cannot import the backend package) is the one no test ever checked
against the backend's set at all. Mirrors ``test_hermes_boot_secret_contract.py``'s own
precedent: put the bridge on ``sys.path`` and compare its literal directly, so a drift between
the two fails HERE instead of shipping green on both sides.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

# The bridge lives outside the backend's own package tree (apps/infra/hermes), so it is not on
# sys.path by default -- put it there before importing bridge.sync. Mirrors
# test_hermes_boot_secret_contract.py's own setup.
_HERMES_ROOT = Path(__file__).resolve().parents[4] / "infra" / "hermes"
if str(_HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(_HERMES_ROOT))

from bridge import sync as bridge_sync  # noqa: E402

from core.services import hermes_profiles  # noqa: E402


def test_bridge_backend_owned_files_matches_the_backends_own_set():
    assert frozenset(bridge_sync.BACKEND_OWNED_FILES) == hermes_profiles.BACKEND_OWNED_FILES


def test_the_facts_filename_is_the_same_literal_on_both_sides():
    assert hermes_profiles.FACTS_FILENAME in bridge_sync.BACKEND_OWNED_FILES
