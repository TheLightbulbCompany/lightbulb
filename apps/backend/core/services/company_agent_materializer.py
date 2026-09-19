"""Render ONE company agent's identity file.

Given a ``PlannedAgent`` (from ``company_package``), produce its ``AGENTS.md`` body.
``company_deploy_service`` writes the result into that agent's S3 profile.

This used to also write skill files onto the owner's EFS workspace and deep-merge an
``agents.list`` entry into ``openclaw.json``; a Hermes profile has neither, so all that
remains is the rendering.
"""

from __future__ import annotations


from core.services.company_package import PlannedAgent
from core.services.paperclip_credential_rule import (
    PAPERCLIP_CREDENTIAL_RULE,
)


def render_agent_identity_md(planned: PlannedAgent) -> str:
    """The ``AGENTS.md`` content for one company agent: the planner's body verbatim, plus the
    appended Paperclip credential rule (see that module's docstring for why the append is
    unconditional). ``company_deploy_service``'s per-agent loop uploads the result via
    ``hermes_profiles.upload_profile_files`` (M6-R33).
    """
    return planned.body_md.rstrip("\n") + "\n" + PAPERCLIP_CREDENTIAL_RULE
