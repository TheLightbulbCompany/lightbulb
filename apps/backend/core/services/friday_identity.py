"""Stage per-agent Paperclip identity files on EFS.

Every agent authenticates to Paperclip AS ITSELF via
``workspace-{agent_id}/paperclip-claimed-api-key.json``, written here before the
container boots so the agent's first Paperclip call has credentials. The file is
write-if-missing (an agent's evolved/rotated copy is never clobbered) except for
the platform-owned ``apiUrl`` field, which ``_refresh_stale_api_url`` brings
current in place.

Best-effort throughout: failures log + emit metrics, never raise. The caller
(``ecs_manager.write_user_configs``) wraps this in try/except so a Paperclip
outage or EFS hiccup never blocks container provision.
"""

from __future__ import annotations

import json
import logging

from core.containers.workspace import Workspace
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)


def _refresh_stale_api_url(
    owner_id: str,
    agent_id: str,
    rel_path: str,
    api_url: str,
    workspace: Workspace,
) -> bool:
    """Bring an EXISTING identity file's ``apiUrl`` up to the platform's current
    Paperclip host, leaving every other field (the agent's own, possibly
    rotated, ``token``) exactly as found.

    ``apiUrl`` is platform config that happens to live in a per-agent file: the
    file is write-if-missing so a reprovision never clobbers a token, but that
    same property stranded every pre-2026-08-30 container on the retired
    ``paperclip.isol8-{env}.local`` host when the Cloud Map namespace moved
    (#2115/#2132) — nothing else in the container carries the URL, so webchat
    Paperclip calls failed DNS fleet-wide. Refreshing the one platform-owned
    field on every provision is what keeps the next rename from doing it again.

    Returns True when the file was rewritten. Best-effort: an unreadable or
    malformed file is left alone (a half-understood file must never be replaced
    with a guess), and a failed write logs + emits a metric, never raises.
    """
    try:
        existing = json.loads(workspace.read_file(owner_id, rel_path))
    except Exception as exc:  # noqa: BLE001 — best-effort, leave the file be
        put_metric("paperclip_identity.refresh.unreadable")
        logger.warning(
            "paperclip_identity: cannot read existing file for owner=%s agent=%s: %s",
            owner_id,
            agent_id,
            exc,
        )
        return False
    if not isinstance(existing, dict) or existing.get("apiUrl") == api_url:
        return False
    try:
        workspace.write_file(owner_id, rel_path, json.dumps({**existing, "apiUrl": api_url}))
    except Exception as exc:  # noqa: BLE001 — best-effort, log + continue
        put_metric("paperclip_identity.refresh.error")
        logger.error(
            "paperclip_identity: apiUrl refresh failed for owner=%s agent=%s: %s",
            owner_id,
            agent_id,
            exc,
        )
        return False
    put_metric("paperclip_identity.refresh.api_url")
    logger.info(
        "paperclip_identity: refreshed apiUrl for owner=%s agent=%s (%s -> %s)",
        owner_id,
        agent_id,
        existing.get("apiUrl"),
        api_url,
    )
    return True


async def stage_paperclip_identity_for_agent(
    *,
    owner_id: str,
    agent_id: str,
    token: str,
    company_id: str | None,
    api_url: str | None,
    workspace: Workspace,
) -> None:
    """Stage ONE agent's per-agent Paperclip identity file on EFS.

    Single-agent counterpart to ``pre_stage_paperclip_identities`` — used by the
    post-create pairing path (``ensure_operator_device_paired``) for agents
    created against an already-running container. That flow never runs
    ``write_user_configs``, so without this the new agent's
    ``paperclip-claimed-api-key.json`` is never written and its first Paperclip
    wake points at a missing credentials file.

    Writes ``workspace-{agent_id}/paperclip-claimed-api-key.json`` with the
    agent's OWN credentials, write-if-missing (idempotent — an agent's evolved
    file isn't clobbered). A missing ``token``/``company_id``/``api_url`` (key
    mint failed or Paperclip unreachable) skips staging; the next reprovision
    self-heals. Best-effort: failures log + emit metrics, never raise.
    """
    if not (token and company_id and api_url):
        put_metric("paperclip_identity.seed.skipped_no_context")
        logger.info(
            "paperclip_identity.skipped: missing token/company_id/api_url for owner=%s agent=%s — deferring",
            owner_id,
            agent_id,
        )
        return

    # Lock the file shape — it's Isol8-defined and read by the seed prompt
    # via `jq -r .token` / .agentId / .companyId / .apiUrl.
    content = json.dumps(
        {
            "token": token,
            "agentId": agent_id,
            "companyId": company_id,
            "apiUrl": api_url,
        }
    )
    rel_path = f"workspace-{agent_id}/paperclip-claimed-api-key.json"
    try:
        wrote = workspace.write_file_if_missing(owner_id, rel_path, content)
    except Exception as exc:  # noqa: BLE001 — best-effort, log + continue
        put_metric("paperclip_identity.seed.error")
        logger.error(
            "paperclip_identity: write failed for owner=%s agent=%s: %s",
            owner_id,
            agent_id,
            exc,
        )
        return
    if wrote:
        put_metric("paperclip_identity.seed.success")
    else:
        put_metric("paperclip_identity.seed.skipped_existing")
        _refresh_stale_api_url(owner_id, agent_id, rel_path, api_url, workspace)
    logger.info(
        "paperclip_identity: owner=%s agent=%s wrote=%s",
        owner_id,
        agent_id,
        wrote,
    )


async def pre_stage_paperclip_identities(
    *,
    owner_id: str,
    paperclip_agents: list[dict],
    agent_keys: dict[str, str],
    company_id: str | None,
    api_url: str | None,
    workspace: Workspace,
) -> None:
    """Stage each agent's per-agent Paperclip identity file on EFS.

    For every agent that has a minted key in ``agent_keys`` we write
    ``workspace-{agent_id}/paperclip-claimed-api-key.json`` containing the
    agent's OWN Paperclip credentials::

        {"token": <key>, "agentId": <id>, "companyId": <company>, "apiUrl": <url>}

    The file lands in the agent's own workspace dir (``workspace-{agent_id}/``
    PLURAL — its runtime cwd; the singular ``workspace`` is container-global and
    collides). The seed prompt instructs the agent to load it before any
    Paperclip call so it authenticates AS ITSELF, not as another agent. Written with
    write-if-missing semantics so an agent's evolved/rotated file isn't
    clobbered on reprovision — except the platform-owned ``apiUrl`` field,
    which ``_refresh_stale_api_url`` brings up to date in place (see it for
    why: the 2026-08-30 Cloud Map rename stranded every existing file).

    Best-effort: per-file failures log + emit metrics and are skipped; this
    never raises. A missing ``company_id``/``api_url`` (Paperclip unreachable
    at provision) skips staging entirely — the next reprovision self-heals.
    """
    if not agent_keys:
        put_metric("paperclip_identity.seed.skipped")
        logger.info(
            "paperclip_identity.skipped: no minted keys for owner=%s — deferring to next provision",
            owner_id,
        )
        return
    if not (company_id and api_url):
        put_metric("paperclip_identity.seed.skipped_no_context")
        logger.info(
            "paperclip_identity.skipped: missing company_id/api_url for owner=%s — deferring",
            owner_id,
        )
        return

    written: list[str] = []
    skipped: list[str] = []
    for agent in paperclip_agents:
        agent_id = agent.get("id")
        if not agent_id:
            continue
        token = agent_keys.get(agent_id)
        if not token:
            continue
        # Lock the file shape — it's Isol8-defined and read by the seed prompt
        # via `jq -r .token` / .agentId / .companyId / .apiUrl.
        content = json.dumps(
            {
                "token": token,
                "agentId": agent_id,
                "companyId": company_id,
                "apiUrl": api_url,
            }
        )
        rel_path = f"workspace-{agent_id}/paperclip-claimed-api-key.json"
        try:
            wrote = workspace.write_file_if_missing(owner_id, rel_path, content)
        except Exception as exc:  # noqa: BLE001 — best-effort, log + continue
            put_metric("paperclip_identity.seed.error")
            logger.error(
                "paperclip_identity: write failed for owner=%s agent=%s: %s",
                owner_id,
                agent_id,
                exc,
            )
            continue
        if wrote:
            written.append(agent_id)
        else:
            skipped.append(agent_id)
            _refresh_stale_api_url(owner_id, agent_id, rel_path, api_url, workspace)

    if written:
        put_metric("paperclip_identity.seed.success", value=len(written))
    if skipped:
        put_metric("paperclip_identity.seed.skipped_existing", value=len(skipped))
    logger.info(
        "paperclip_identity: owner=%s wrote=%s skipped=%s",
        owner_id,
        written,
        skipped,
    )
