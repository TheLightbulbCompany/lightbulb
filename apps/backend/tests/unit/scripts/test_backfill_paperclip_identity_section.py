"""Tests for the one-shot Paperclip-identity backfill.

DELETE alongside the script once it has run against dev and prod.
"""

from scripts.backfill_paperclip_identity_section import (
    _LEGACY_HEADING,
    _NEW_HEADING,
    _new_section,
    rewrite,
)

_LEGACY = f"""{_LEGACY_HEADING}

Your Paperclip credentials live in `./paperclip-claimed-api-key.json`.

```
export PAPERCLIP_API_KEY=$(jq -r .token paperclip-claimed-api-key.json)
```

## Next section

Kept to prove the replacement is bounded.
"""


def test_rewrites_the_known_legacy_block():
    new, reason = rewrite("# AGENTS.md\n\nIntro.\n\n" + _LEGACY)
    assert reason == "rewritten"
    assert "jq -r .token" not in new
    assert "paperclip-claimed-api-key.json" not in new
    assert _NEW_HEADING in new
    # Bounded on both sides: the owner's surrounding content survives.
    assert new.startswith("# AGENTS.md\n\nIntro.")
    assert "## Next section" in new
    assert "Kept to prove the replacement is bounded." in new


def test_is_a_noop_on_an_already_healed_file():
    new, reason = rewrite("# AGENTS.md\n\n" + _new_section())
    assert new is None
    assert reason == "already healed"


def test_reports_rather_than_guesses_when_the_section_is_unrecognised():
    """AGENTS.md is a living playbook. A file mentioning the key file outside
    the known section is surfaced for a human, never regex-surgered — guessing
    at boundaries is how an owner's own writing gets deleted."""
    odd = "# AGENTS.md\n\n## Troubleshooting\n\nIf auth fails, check paperclip-claimed-api-key.json.\n"
    new, reason = rewrite(odd)
    assert new is None
    assert reason.startswith("MANUAL")


def test_leaves_a_file_with_no_paperclip_content_alone():
    new, reason = rewrite("# AGENTS.md\n\nNothing to do here.\n")
    assert new is None
    assert reason == "nothing to do"


def test_rewrite_is_idempotent():
    once, _ = rewrite("# AGENTS.md\n\n" + _LEGACY)
    twice, reason = rewrite(once)
    assert twice is None
    assert reason == "already healed"
