"""Cross-language conformance test for the seeded starter-agent names.

``STARTER_ROSTER`` (backend) decides what a fresh workspace's six agents are
called. ``STARTER_AGENT_DEFAULT_NAMES`` in
``packages/shared/src/messaging/onboardingSteps.ts`` holds the same six
strings, and the onboarding setup band uses them to answer one question: has
anybody named this team yet? An agent whose name is NOT in that set has been
renamed, which marks the "Name your agents" card done.

Drift is silent on both sides. Rename a role in ``STARTER_ROSTER`` and the
frontend stops recognising it as a default, so a brand-new workspace looks
already-named and never sees the card. Nothing type-checks across that seam, so
the check lives here -- read the other language's literal and compare, the same
way ``test_message_attachment_contract.py`` does for the attachment allowlist.

CLAUDE.md names this hazard class: "Lockstep constants -- NEVER move one alone."
"""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.workspace_provisioning import STARTER_ROSTER  # noqa: E402

# apps/backend/tests/unit/contracts/<this file> -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[5]
_ONBOARDING_STEPS = _REPO_ROOT / "packages" / "shared" / "src" / "messaging" / "onboardingSteps.ts"


def _frontend_default_names() -> list[str]:
    assert _ONBOARDING_STEPS.is_file(), f"onboarding steps not found at {_ONBOARDING_STEPS}"
    source = _ONBOARDING_STEPS.read_text(encoding="utf-8")
    match = re.search(
        r"export const STARTER_AGENT_DEFAULT_NAMES: ReadonlySet<string> = new Set\(\[(.*?)\]\);",
        source,
        re.DOTALL,
    )
    assert match, "could not find STARTER_AGENT_DEFAULT_NAMES in onboardingSteps.ts"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_frontend_knows_every_name_the_backend_seeds():
    assert set(_frontend_default_names()) == {name for _key, name, *_rest in STARTER_ROSTER}


def test_neither_side_carries_a_name_the_other_does_not():
    """Set equality above would pass on a duplicate; this pins the count too."""
    assert len(_frontend_default_names()) == len(STARTER_ROSTER)
