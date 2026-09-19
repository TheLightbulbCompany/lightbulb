"""The per-agent company materializer: render ONE agent's identity file.

It used to also write skill assets onto the owner's EFS and deep-merge an ``agents.list``
entry into ``openclaw.json``, and its defining property was what that entry did NOT carry —
no ``subagents.allowAgents``, no ``tools.deny`` gateway clamp, so company agents could
delegate along the package's reportsTo graph. There is no such entry on a Hermes profile: a
profile's ``config.yaml`` is per-agent by construction, so containment is not something a
write here could accidentally impose. The write itself is
``company_deploy_service``'s per-agent ``hermes_profiles.upload_profile_files`` loop, covered
in that module's tests.

What remains here is the rendering, and the one invariant it carries: the Paperclip
credential rule is appended unconditionally.
"""

from core.services.company_agent_materializer import render_agent_identity_md
from core.services.company_package import PlannedAgent
from core.services.paperclip_credential_rule import PAPERCLIP_CREDENTIAL_RULE


def _planned(**over) -> PlannedAgent:
    base = dict(
        slug="cto",
        name="Ada",
        role="member",
        title="Chief Technology Officer",
        body_md="# Ada\nYou are the CTO. Delegate freely.\n",
        reports_to_slug="ceo",
        skills=["code-review", "deploy"],
        capabilities="engineering leadership",
    )
    base.update(over)
    return PlannedAgent(**base)


def test_the_planners_body_rides_through_verbatim():
    body = render_agent_identity_md(_planned())
    assert body.startswith("# Ada\nYou are the CTO. Delegate freely.\n")


def test_the_credential_rule_is_appended_unconditionally():
    """See ``paperclip_credential_rule``'s docstring for why it is not conditional: an agent
    that has not been told the rule will paste a credential into a Paperclip comment."""
    assert render_agent_identity_md(_planned()).endswith(PAPERCLIP_CREDENTIAL_RULE)
    assert render_agent_identity_md(_planned(skills=None)).endswith(PAPERCLIP_CREDENTIAL_RULE)
    assert render_agent_identity_md(_planned(reports_to_slug=None)).endswith(PAPERCLIP_CREDENTIAL_RULE)


def test_exactly_one_newline_joins_the_body_and_the_rule():
    """The planner's body may or may not end in a newline; either way the join must not run
    the rule onto the last line of the identity or leave a blank stanza between them."""
    for body_md in ("# Ada\nno trailing newline", "# Ada\none\n", "# Ada\nmany\n\n\n"):
        rendered = render_agent_identity_md(_planned(body_md=body_md))
        assert rendered == body_md.rstrip("\n") + "\n" + PAPERCLIP_CREDENTIAL_RULE
