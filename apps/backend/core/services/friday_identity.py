"""Stage per-agent Paperclip identity files (and the workspace-state seed) on EFS.

Every agent authenticates to Paperclip AS ITSELF via
``workspace-{agent_id}/paperclip-claimed-api-key.json``, written here before the
container boots so the agent's first Paperclip call has credentials. The file is
write-if-missing (an agent's evolved/rotated copy is never clobbered) except for
the platform-owned ``apiUrl`` field, which ``_refresh_stale_api_url`` brings
current in place.

``seed_workspace_state`` seeds OpenClaw's workspace-setup-state sentinel into a
workspace this provision is CREATING -- see its docstring for why that write has
to stay one-shot (#1429).

Best-effort throughout: failures log + emit metrics, never raise. The caller
(``ecs_manager.write_user_configs``) wraps this in try/except so a Paperclip
outage or EFS hiccup never blocks container provision.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.containers.workspace import Workspace
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# The two seed formats (contract mirrors the fork's boot hook — see the
# `upstream` notes in openclaw-version.json for which fork patch adds it):
#   - NATIVE sentinel — what we WRITE. A non-legacy file the fork's boot hook
#     feeds through its own idempotent ``mergeWorkspaceSetupState`` upsert, then
#     deletes. Its name MUST NOT be a legacy source path
#     (``resolveLegacyWorkspaceSourcePaths``) — the presence-only legacy gate
#     ignores it, so the #1429 error class is structurally impossible.
#   - LEGACY — RETIRED, never written any more. Kept only so the heal branch can
#     still clear a stale copy from a workspace seeded before this change. It is
#     the format OpenClaw's boot doctor migrates into SQLite (recording a
#     sha256-of-bytes receipt) then deletes — fragile on re-seed, which is
#     exactly the #1429 brick (see seed_workspace_state's docstring).
_LEGACY_STATE_REL = ".openclaw/workspace-state.json"
_NATIVE_SEED_REL = ".openclaw/isol8-setup-seed.json"


def js_iso_now() -> str:
    """JS-canonical ISO-8601 UTC timestamp (``2026-07-23T06:36:32.300Z``).

    OpenClaw 7.2 migrates our seeded ``workspace-state.json`` into its SQLite
    state store and validates every timestamp with JS
    ``new Date(v).toISOString() === v`` — exactly 3 fractional digits and a
    literal ``Z``. Python's ``.isoformat()`` (6-digit microseconds, ``+00:00``)
    fails that round-trip, the boot doctor logs ``Failed reading legacy
    workspace state: legacy workspace setup bootstrap timestamp is invalid``,
    the migration aborts, and every agent run then throws ``Legacy workspace
    setup state requires migration``. Keep this format byte-identical to
    ``Date.prototype.toISOString``.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def seed_workspace_state(workspace: Workspace, owner_id: str, agent_uuid: str) -> str:
    """One-shot seed of the workspace-setup-state marker.

    Returns the action taken: ``"seeded"`` (fresh workspace, file written),
    ``"skipped"`` (existing workspace left alone), or ``"deleted_stale"``
    (existing workspace healed of a re-seeded LEGACY copy — see below).

    FORMAT — the NATIVE sentinel, unconditionally:
    ``workspace-{agent_uuid}/.openclaw/isol8-setup-seed.json`` containing
    ``{"bootstrapSeededAt", "setupCompletedAt"}``. The fork's boot hook
    (``ensureAgentWorkspace``, v2026.7.2-isol8.2+) converts it via
    ``mergeWorkspaceSetupState`` then deletes it, so the legacy presence gate
    never sees a legacy file and the #1429 brick class is structurally
    impossible. The legacy ``workspace-state.json`` is NO LONGER WRITTEN.

    The heal branch below still targets the LEGACY file only: it clears a stale
    legacy JSON on an already-migrated workspace (a container seeded before this
    change), never the native sentinel — which the fork itself removes after
    converting.

    WHY ONE-SHOT (#1429, mechanism verified against the deployed fork tag
    v2026.7.2-isol8.1) — this history applies to the LEGACY format we no longer
    write; the native sentinel is a safe idempotent upsert, but the fresh-only
    gate is kept so the write stays a genuine one-shot:

    OpenClaw 7.2 treats this file as a RETIRED legacy source. The runtime
    never parses it — ``assertNoUnmigratedWorkspaceState``
    (``src/agents/workspace-legacy-state.ts``) is a PRESENCE-ONLY gate hit on
    every agent run: if the file exists at all, every run throws ``Legacy
    workspace setup state requires migration … run openclaw doctor --fix``.
    The doctor-on-boot pass (``state-migrations.workspace-setup.ts``) is what
    keeps a fresh seed working: on the FIRST boot it imports the file into the
    shared SQLite state store (``~/.openclaw/state/openclaw.sqlite``), records
    a receipt carrying the file's sha256, and REMOVES the file.

    That removal is what used to re-arm the bug: the paid-conversion
    reprovision re-ran this seeder, ``write_file_if_missing`` found the
    migrated-and-removed path empty, and re-wrote the file with a fresh
    timestamp. On the next boot doctor found receipt + file, compared sha256s,
    got a mismatch, and REFUSED cleanup ("Workspace state is in SQLite, but
    the retired source now conflicts." — ``cleanupReceiptSource``) — leaving
    the file in place for the runtime gate to throw on, permanently. Even
    ``doctor --fix`` cannot clear that state. Hence: seed ONLY when the
    workspace directory does not exist yet (this provision is creating it);
    an existing workspace is never re-seeded, under any flag.

    HEALING ALREADY-BRICKED WORKSPACES: on an existing workspace, if the file
    is present AND the shared state DB exists (the container has booted 7.2's
    doctor at least once — a seeded workspace always predates the first
    boot, so a coexisting DB means this file post-dates a migration pass or a
    mid-migration crash), DELETE it. Provably safe per the upstream header
    ("Runtime state never parses these files"): canonical state lives in
    SQLite, the file's only runtime effect is tripping the presence gate, and
    removal is exactly what doctor's own cleanup would do if the sha matched.
    In the crash-window case the cost is bounded to losing a best-effort
    marker whose ABSENCE is the normal vanilla-workspace path. When the DB
    does not exist (container never booted since seeding) the file is the
    original un-migrated seed and is left for the first boot to migrate.
    """
    legacy_rel = f"workspace-{agent_uuid}/{_LEGACY_STATE_REL}"
    workspace_dir = workspace.agent_workspace_path(owner_id, agent_uuid)
    if not workspace_dir.is_dir():
        now_iso = js_iso_now()
        # Native seed, unconditionally: drop the non-legacy sentinel the fork
        # converts + deletes at boot. Same field VALUES as the old legacy seed,
        # minus the legacy-only ``version`` key. The legacy file is NEVER
        # written any more, so the #1429 presence gate has nothing to trip on.
        #
        # Requires the isol8.2+ base image (v2026.7.2-isol8.2, the boot hook in
        # ``ensureAgentWorkspace``) — deployed to dev AND prod as
        # ``2026.7.2-isol8.2-5fecaee`` before this became unconditional. On an
        # older image the sentinel is simply ignored and the workspace derives
        # its setup state from filesystem evidence as it always did.
        workspace.write_file_if_missing(
            owner_id,
            f"workspace-{agent_uuid}/{_NATIVE_SEED_REL}",
            json.dumps({"bootstrapSeededAt": now_iso, "setupCompletedAt": now_iso}),
        )
        return "seeded"
    shared_state_db = workspace.user_path(owner_id) / "state" / "openclaw.sqlite"
    if (workspace_dir / ".openclaw" / "workspace-state.json").is_file() and shared_state_db.is_file():
        workspace.delete_file(owner_id, legacy_rel)
        return "deleted_stale"
    return "skipped"


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
