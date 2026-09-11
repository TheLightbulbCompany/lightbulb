"""The seeded playbook rule must forbid printing the Paperclip key.

Paperclip wakes an agent with fork-side text that still says to load the key
from a file (``claimed_api_key_path``). A playbook that omits the prohibition
produces an agent that follows that instruction and prints a live key into its
visible reasoning, which is the 2026-08-14 leak. The company-agent materializer
is the seed path that writes a model-authored body, so the rule cannot be left
to the planner -- it appends it.
"""

from unittest.mock import MagicMock

from core.services import company_agent_materializer
from core.services.paperclip_credential_rule import (
    PAPERCLIP_CREDENTIAL_PROHIBITION,
    PAPERCLIP_CREDENTIAL_RULE,
)


def test_the_rule_itself_names_the_vars_but_forbids_printing_the_key():
    assert PAPERCLIP_CREDENTIAL_PROHIBITION in PAPERCLIP_CREDENTIAL_RULE
    # The agent still has to know the credentials exist — but never write the
    # header itself, so the var name is named without a leading `$` now that
    # the fence routes through paperclip-api.sh instead of a raw curl.
    assert "PAPERCLIP_API_KEY" in PAPERCLIP_CREDENTIAL_RULE
    assert "$PAPERCLIP_API_URL" in PAPERCLIP_CREDENTIAL_RULE
    assert "Authorization: Bearer $" not in PAPERCLIP_CREDENTIAL_RULE
    # And must be told to ignore the stale fork-side wake instruction.
    assert "ignore" in PAPERCLIP_CREDENTIAL_RULE
    # It must never itself demonstrate reading the file.
    assert "jq -r .token" not in PAPERCLIP_CREDENTIAL_RULE
    assert "paperclip-claimed-api-key.json" not in PAPERCLIP_CREDENTIAL_RULE

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
