"""Bind each agent to a specific Google account for the ``gog`` CLI.

An agent that acts on Gmail / Calendar / Drive must do so as a *specific*
Google account. The binding reflects ONLY the agent's EXPLICIT assignment
(``google_accounts_repo.get_assignment``) — there is no default fallback. An
unassigned agent gets no binding and uses bare ``gog`` (the keyring's own
account). Deriving it from a default would leave a stale binding behind when
that default account is later disconnected, since an unassigned agent has no
row to drive a cleanup.

The assignment is written as ``GOG_ACCOUNT`` — gog's own default-account
variable — into the agent's Hermes profile ``cli-env.json``, which the bridge
delivers to the turn's shell. No agent cooperation, nothing to read.

Seeding is **user-action-driven**: the only callers are the assign API (which
seeds one agent when its account changes) and the disconnect path (which
re-seeds affected agents when an account goes away).
"""

from __future__ import annotations

from core.observability.metrics import put_metric
from core.repositories import google_accounts_repo


async def seed_agent_google_identity(owner_id: str, agent_id: str) -> None:
    """Write (or clear) the agent's ``GOG_ACCOUNT`` profile env value."""
    from core.services.credentials import hermes_cli

    email = await google_accounts_repo.get_assignment(owner_id, agent_id)
    await hermes_cli.set_profile_cli_env(owner_id, agent_id, {"GOG_ACCOUNT": email})
    put_metric("google_identity.seed.cleared" if email is None else "google_identity.seed.applied")
