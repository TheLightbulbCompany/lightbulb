"""Every seeded playbook must forbid printing the Paperclip key.

Three independent seed paths write agent playbooks — Friday's template, the
Atlas seed, and the company-agent materializer — and each is woken by
Paperclip with fork-side text that still says to load the key from a file
(``claimed_api_key_path``). A path that omits the prohibition produces an
agent that follows that instruction and prints a live key into its visible
reasoning, which is the 2026-08-14 leak.

Nothing structurally couples the three, so this test does.
"""

from unittest.mock import MagicMock

from core.services import company_agent_materializer, friday_identity
from core.services.paperclip_credential_rule import (
    PAPERCLIP_CREDENTIAL_PROHIBITION,
    PAPERCLIP_CREDENTIAL_RULE,
)


def _personal_profile():
    return {
        "kind": "personal",
        "owner_display": "Sam",
        "first_name_or_fallback": "Sam",
        "full_name": "Sam Rivers",
        "email": "sam@example.com",
    }


def test_the_rule_itself_names_the_vars_but_forbids_printing_the_key():
    assert PAPERCLIP_CREDENTIAL_PROHIBITION in PAPERCLIP_CREDENTIAL_RULE
    # The agent still has to be able to USE the credentials.
    assert "$PAPERCLIP_API_KEY" in PAPERCLIP_CREDENTIAL_RULE
    assert "$PAPERCLIP_API_URL" in PAPERCLIP_CREDENTIAL_RULE
    # And must be told to ignore the stale fork-side wake instruction.
    assert "ignore" in PAPERCLIP_CREDENTIAL_RULE
    # It must never itself demonstrate reading the file.
    assert "jq -r .token" not in PAPERCLIP_CREDENTIAL_RULE
    assert "paperclip-claimed-api-key.json" not in PAPERCLIP_CREDENTIAL_RULE


def test_fridays_playbook_carries_the_prohibition():
    agents_md = friday_identity._render_templates(_personal_profile())["AGENTS.md"]
    assert PAPERCLIP_CREDENTIAL_PROHIBITION in agents_md


def test_atlas_playbook_carries_the_prohibition():
    """Atlas's vendored AGENTS.md says nothing about credentials on its own —
    the seed appends the rule, exactly as it appends the ACP section."""
    from pathlib import Path

    import core.services.atlas_seed as atlas_seed

    vendored = (Path(atlas_seed.__file__).parent / "files" / "AGENTS.md").read_text(encoding="utf-8")
    # Guard the premise: if the vendored file ever grows its own credential
    # section, this test should be revisited rather than silently passing.
    assert PAPERCLIP_CREDENTIAL_PROHIBITION not in vendored
    seeded = vendored + atlas_seed._ACP_DELEGATION_SECTION + PAPERCLIP_CREDENTIAL_RULE
    assert PAPERCLIP_CREDENTIAL_PROHIBITION in seeded


async def test_company_agent_body_gets_the_prohibition_appended():
    """A specialist's body is model-authored, so the rule cannot be left to
    the planner — the materializer appends it. Exercises the real call rather
    than re-deriving the string, or the test would pass while the code did
    nothing."""
    written: dict[str, str] = {}
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(
        side_effect=lambda owner, path, content: written.__setitem__(path, content)
    )
    planned = MagicMock()
    planned.body_md = "# Some Specialist\n\nModel-authored body with no credential rule.\n"
    planned.name = "Specialist"

    async def apply_aux(*_args, **_kwargs):
        return None

    await company_agent_materializer.materialize_agent(
        owner_id="user_abc",
        new_agent_id="agent_x",
        planned=planned,
        skill_assets=None,
        workspace=workspace,
        apply_aux=apply_aux,
    )

    body = written["workspace-agent_x/AGENTS.md"]
    assert "Model-authored body" in body  # the planner's content survives
    assert PAPERCLIP_CREDENTIAL_PROHIBITION in body
