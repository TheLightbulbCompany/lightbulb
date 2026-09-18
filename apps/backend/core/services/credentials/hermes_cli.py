"""Project a CLI-materializer's credentials into every Hermes profile — the replacement for
ruling M6-R24's "exec runs on the owner's Mac, no Hermes equivalent" skip.

M6-R24 was written when agent shell commands were going to run on the owner's paired Mac
(``terminal.backend: lightbulb_node``). That is no longer true: #2749 made every Hermes turn
execute LOCALLY inside the AgentCore microVM, so the five CLI-materializer connectors
(``aws_cli``, ``gh_cli``, ``gog_cli``, ``instagram_cli``, ``xurl_cli``) once again need their
credential files next to the shell that uses them.

**How it works — the materializers are reused verbatim.** Each of them already writes a
*home-shaped* tree under ``owner_efs_root(owner)`` (``.aws/``, ``.config/gh/``,
``.config/gogcli`` + ``.local/share/gogcli``, ``skills/instagram/``, ``.xurl``) — and a Hermes
profile dir IS a home. So instead of a second set of per-provider writers, this module points
``owner_efs_root`` at a throwaway directory (``materializers.base.materialize_into``), runs the
REAL materializer — gog included, keyring binary and all — and uploads whatever landed into
every roster profile in S3. Two materializers branch on ``base.is_projecting()`` because they do
something other than write under the home: ``aws_cli``'s in-container helper path and
``instagram_cli``'s openclaw.json skills switch.

**Per-agent, like mcp-tokens (M4-R47).** ``sync.ensure_profile``/``restore_profile`` only ever
read the agent's OWN prefix, so an owner-level object would never reach a microVM. Every roster
profile therefore gets an identical copy. Unlike mcp-tokens this is NOT write-once: a connect is
an explicit owner action delivering a fresh credential and must win, exactly as the ECS rail's
``write`` overwrites the EFS copy. The cost is that a token one agent's container has since
rotated in place is replaced by the newly connected one on the next connect/disconnect — the
same thing that happens on ECS, where all agents share one file.

**How the shell finds them:** ``bridge/turn.py::_terminal_scope``'s ``env_overlay`` sets each
CLI's own config-location variable (``AWS_CONFIG_FILE``, ``GH_CONFIG_DIR``,
``XDG_CONFIG_HOME``/``XDG_DATA_HOME``, and ``HOME`` — xurl's only lever) to point inside the
profile. Nothing here mutates ``os.environ``.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from core.config import settings
from core.dynamodb import run_in_thread
from core.services.credentials.materializers import MATERIALIZER_REMOVERS, MATERIALIZERS, base

logger = logging.getLogger(__name__)

#: Which paths inside the home-shaped tree each materializer OWNS. Three jobs: seed the scratch
#: root before running the materializer (so a shared file keeps the entries this provider does
#: not own), decide what to upload, and decide what to delete when the materializer removed it.
#: A materializer absent from this map has no Hermes projection and keeps the old skip.
#: ``mcp_oauth``/``qbo_dotenv`` are deliberately absent: they are MCP-shaped and already ride
#: ``reconcile_integrations``. ``toml_cli``/``skill_cli`` are absent because no live provider in
#: ``providers.yaml`` uses them for a shell CLI the microVM has a binary for.
OWNED_PATHS: dict[str, tuple[str, ...]] = {
    "aws_cli": (".aws/",),
    "gh_cli": (".config/gh/",),
    "gog_cli": (".config/gogcli/", ".local/share/gogcli/"),
    "instagram_cli": ("skills/instagram/",),
    "xurl_cli": (".xurl",),
}

#: Backend-owned values the projected CLI needs to see in the SHELL's environment, written to
#: the profile's ``cli-env.json`` and merged into the terminal overlay by the bridge.
#: Deliberately NOT the profile ``.env``: that file feeds Hermes's in-process secret scope,
#: which this module's own docstring in ``hermes_profiles`` records as never touching
#: ``os.environ`` — so a subprocess would never see it. gog is the only member: its file keyring
#: is a password-derived JWE blob and the container's gog must decrypt it with the SAME shared
#: password the backend encrypted it with (on ECS that arrives as a task-def secret).
CLI_ENV_FILE = "cli-env.json"


def _shell_env(materializer: str) -> dict[str, str]:
    if materializer == "gog_cli" and settings.GOG_KEYRING_PASSWORD:
        return {"GOG_KEYRING_PASSWORD": settings.GOG_KEYRING_PASSWORD, "GOG_KEYRING_BACKEND": "file"}
    return {}


def supports(materializer: str | None) -> bool:
    """Whether connecting this materializer's provider projects into a Hermes profile."""
    return bool(materializer) and materializer in OWNED_PATHS


def _snapshot(root: Path, materializer: str) -> dict[str, bytes]:
    """Every file currently under this materializer's owned paths, keyed by its path relative
    to the home root — which is exactly its key suffix inside the profile prefix."""
    found: dict[str, bytes] = {}
    for owned in OWNED_PATHS[materializer]:
        target = root / owned.rstrip("/")
        if target.is_file():
            found[owned.rstrip("/")] = target.read_bytes()
        elif target.is_dir():
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _run_and_sync(
    owner_id: str,
    agent_ids: list[str],
    provider_key: str,
    materializer: str,
    credential: dict[str, Any],
    write: bool,
) -> Any:
    """Sync (boto3 + subprocess + the materializer's own blocking HTTP) — always called through
    ``run_in_thread``, matching ``refresh_integrations``'s per-agent S3 convention."""
    import json

    from core.services import hermes_profiles

    owned = OWNED_PATHS[materializer]
    fn = (MATERIALIZERS if write else MATERIALIZER_REMOVERS)[materializer]
    with tempfile.TemporaryDirectory(prefix="hermes-cli-") as tmp:
        root = Path(tmp)
        # Seed from the first profile: the backend writes every profile identically, and the
        # materializer needs the CURRENT file to preserve the entries it does not own.
        hermes_profiles.fetch_profile_subtree(owner_id, agent_ids[0], owned, root)
        before = set(_snapshot(root, materializer))
        with base.materialize_into(root):
            result = fn(owner_id, provider_key, credential)  # type: ignore[operator]
        files = _snapshot(root, materializer)
        shell_env = _shell_env(materializer) if write else {}
        if shell_env:
            files[CLI_ENV_FILE] = json.dumps(shell_env).encode()
        gone = before - set(files)
        for agent_id in agent_ids:
            hermes_profiles.write_profile_subtree(owner_id, agent_id, files, gone)
    return result


async def _roster(owner_id: str) -> list[str]:
    from core.services.messaging import load_agent_roster

    return [agent["id"] for agent in await load_agent_roster(owner_id)]


async def project(owner_id: str, provider_key: str, materializer: str, credential: dict[str, Any]) -> Any:
    """Connect/reconnect. Returns the materializer's own identity value (gog's account email,
    xurl's handle) so ``apply_connection``'s contract is unchanged. Deliberately does NOT
    swallow: there is no later re-assert pass for this rail, so ``connect_paste``'s
    ``apply_failed`` rollback is the only thing between a failed projection and a row that
    reads "connected" with no working credential — the same reasoning as M6-R31's
    ``_project_hermes_openclaw_paths``."""
    return await run_in_thread(
        _run_and_sync, owner_id, await _roster(owner_id), provider_key, materializer, credential, True
    )


async def clear(owner_id: str, provider_key: str, materializer: str, credential: dict[str, Any]) -> None:
    """Disconnect twin. Runs the materializer's own ``remove`` against a scratch copy of the
    current profile state, then syncs the result — so gog dropping ONE account keeps the rest of
    the keyring and xurl dropping the ``lightbulb-x`` app keeps the owner's other apps, exactly
    as on EFS. Best-effort: the row has already committed its disconnect by the time this runs
    and there is no rollback to trigger (same convention as the MCP and openclaw_paths
    disconnect branches)."""
    if not supports(materializer):
        return
    try:
        await run_in_thread(
            _run_and_sync, owner_id, await _roster(owner_id), provider_key, materializer, credential, False
        )
    except Exception:  # noqa: BLE001 — a disconnect must never fail because the projection did
        logger.exception("agentcore: hermes CLI projection clear failed owner=%s provider=%s", owner_id, provider_key)
