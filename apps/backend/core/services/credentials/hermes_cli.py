"""Project a CLI-materializer's credentials into every Hermes profile — the replacement for
ruling M6-R24's "exec runs on the owner's Mac, no Hermes equivalent" skip.

M6-R24 was written when agent shell commands were going to run on the owner's paired Mac
(``terminal.backend: lightbulb_node``). That is no longer true: #2749 made every Hermes turn
execute LOCALLY inside the AgentCore microVM, so the five CLI-materializer connectors
(``aws_cli``, ``gh_cli``, ``gog_cli``, ``instagram_cli``, ``xurl_cli``) once again need their
credential files next to the shell that uses them.

**How it works — the materializers are reused verbatim.** Each of them already writes a
*home-shaped* tree under ``owner_efs_root(owner)`` (``.aws/``, ``.config/gh/``,
``.config/gogcli`` + ``.local/share/gogcli``, ``.xurl``) — because on ECS the per-user EFS
access point roots the owner at ``/home/node/.openclaw``. So instead of a second set of
per-provider writers, this module points ``owner_efs_root`` at a throwaway directory
(``materializers.base.materialize_into``), runs the REAL materializer — gog included, keyring
binary and all — and uploads whatever landed into every roster profile in S3. Two materializers
branch on ``base.is_projecting()`` because they do something other than write under the home:
``aws_cli``'s in-container helper path and ``instagram_cli``'s openclaw.json skills switch.

**Where it lands: the profile's own ``home/``.** ``$HOME`` inside the microVM is `/` by default
(``hermes_constants.get_subprocess_home``'s ``auto`` mode only yields ``{HERMES_HOME}/home``
when that directory exists). The bridge sets ``TERMINAL_HOME_MODE: profile`` and creates
``<profile>/home``, and Hermes's home override makes ``HERMES_HOME`` the profile dir for the
turn — so ``$HOME`` is ``<profile>/home`` and a dotfile tree projected there is exactly where
every CLI's own default lookup goes. ``instagram_cli`` is the exception: its output is a Hermes
*skills* dir, which is profile-relative, not a home dotfile, so it keeps ``skills/instagram/``.

**Per-agent, like mcp-tokens (M4-R47).** ``sync.ensure_profile``/``restore_profile`` only ever
read the agent's OWN prefix, so an owner-level object would never reach a microVM. Every roster
profile therefore gets an identical copy.

**That identity is LOAD-BEARING, not an implementation detail.** ``$HOME`` inside the microVM
freezes at the first profile to run a shell in the process (``agent.secret_scope``'s
``_GLOBAL_ENV_EXACT``), so anything reached through ``$HOME`` rather than through one of
``sync.cli_credential_env``'s path variables — ``xurl``'s ``~/.xurl``, and ``aws_cli``'s helper
beside its ``token``/``cache.json`` — can be read from a SIBLING profile. That is harmless only
because the bytes there are the same: these are OWNER-level credentials and ``_run_and_sync``
fans ONE ``files`` dict out to the whole roster. **Adding a materializer to ``OWNED_PATHS``/
``ROOT_DIR`` whose output differs per agent breaks that, and turns a sibling read into one
agent reading another's credential.** Such a materializer needs a per-profile path variable on
the passthrough rail first. Unlike mcp-tokens this is NOT write-once: a connect is
an explicit owner action delivering a fresh credential and must win, exactly as the ECS rail's
``write`` overwrites the EFS copy. A token the container rotates IN PLACE is NOT merely
"replaced on the next connect": for a single-use rotating refresh token (X via ``.xurl``) the
first in-VM refresh kills the copy in S3, so the bridge writes that rotation back to every
roster profile (``sync.save_rotated_cli_credentials``, If-Match on the ETag it replaced — a
connect/disconnect landing first still wins). Covered: X's ``.xurl`` and QuickBooks'
``quickbooks/.env`` (the bridge's ``ROTATING_CLI_CREDENTIALS``).
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Any

from core.config import settings
from core.dynamodb import run_in_thread
from core.services.credentials import registry
from core.services.credentials.materializers import MATERIALIZER_REMOVERS, MATERIALIZERS, base

logger = logging.getLogger(__name__)

#: Which paths inside the home-shaped tree each materializer OWNS, relative to the root that
#: materializer writes into. Three jobs: seed the scratch root before running the materializer
#: (so a shared file keeps the entries this provider does not own), decide what to upload, and
#: decide what to delete when the materializer removed it. A materializer absent from this map
#: has no Hermes projection and keeps the old skip. ``mcp_oauth`` is such a name: it survives in
#: providers.yaml only as a rail marker (remote grants use the broker/native MCP rails) and has
#: no implementation in ``MATERIALIZERS``.
OWNED_PATHS: dict[str, tuple[str, ...]] = {
    "aws_cli": (".aws/",),
    "gh_cli": (".config/gh/",),
    "gog_cli": (".config/gogcli/", ".local/share/gogcli/"),
    "instagram_cli": ("skills/instagram/",),
    "qbo_dotenv": ("quickbooks/",),
    "skill_cli": ("skills/vercel/",),
    "shopify_cli": ("skills/shopify/",),
    "xurl_cli": (".xurl",),
}

#: Where each materializer's own root lands inside the profile. The four dotfile CLIs go under
#: ``home/`` — the profile's ``$HOME`` under ``TERMINAL_HOME_MODE: profile``. ``instagram_cli``
#: is absent because its output is a Hermes skills dir, already profile-relative.
HOME_DIR = "home"
ROOT_DIR: dict[str, str] = dict.fromkeys(("aws_cli", "gh_cli", "gog_cli", "qbo_dotenv", "xurl_cli"), HOME_DIR)

#: Backend-owned values a projected CLI needs in the SHELL's environment, written to the
#: profile's ``cli-env.json``. NOT the profile ``.env``: that file feeds Hermes's in-process
#: secret scope, which ``hermes_profiles``'s own module docstring records as never touching
#: ``os.environ`` — a subprocess would never see it. gog is the only member: its file keyring is
#: a password-derived JWE blob and the container's gog must decrypt it with the SAME shared
#: password the backend encrypted it with (on ECS that arrives as a task-def secret).
#:
#: It is a VALUE STORE, not a delivery mechanism. Delivery is the bridge's own
#: `sync.cli_credential_env`, which reads this file into the turn's secret scope (the names
#: are then registered with Hermes's per-session env passthrough by `turn._allow_child_env_names`).
CLI_ENV_FILE = "cli-env.json"

#: ``cli-env.json`` keys bound to ONE agent (``set_profile_cli_env``), which a new profile must not
#: inherit from a sibling.
PER_AGENT_ENV_KEYS = frozenset({"GOG_ACCOUNT"})


def _shell_env(materializer: str, provider_key: str, credential: dict[str, Any]) -> dict[str, str]:
    if materializer == "gog_cli" and settings.GOG_KEYRING_PASSWORD:
        return {"GOG_KEYRING_PASSWORD": settings.GOG_KEYRING_PASSWORD, "GOG_KEYRING_BACKEND": "file"}
    if materializer == "skill_cli":
        spec = registry.get_provider(provider_key).skill
        token = credential.get("token")
        if spec and token:
            return {spec.primary_env: str(token)}
    return {}


def _shell_env_keys(materializer: str, provider_key: str) -> tuple[str, ...]:
    """The ``cli-env.json`` keys this provider owns — known without a credential, so a
    disconnect can remove exactly its own entries and nobody else's."""
    if materializer == "gog_cli":
        return ("GOG_KEYRING_PASSWORD", "GOG_KEYRING_BACKEND")
    if materializer == "skill_cli":
        spec = registry.get_provider(provider_key).skill
        return (spec.primary_env,) if spec else ()
    return ()


def supports(materializer: str | None) -> bool:
    """Whether connecting this materializer's provider projects into a Hermes profile."""
    return bool(materializer) and materializer in OWNED_PATHS


def profile_paths(materializer: str) -> tuple[str, ...]:
    """``OWNED_PATHS`` rebased onto the profile — what the S3 keys and the bridge's
    ``CLI_CREDENTIAL_PREFIXES`` actually use."""
    root = ROOT_DIR.get(materializer, "")
    return tuple(f"{root}/{owned}" if root else owned for owned in OWNED_PATHS[materializer])


#: N3: the seed -> materialize -> upload cycle is a read-modify-write on files that hold MORE
#: than one entry (gog's keyring holds every Google account; ``.xurl`` holds the owner's other
#: apps). On EFS those are serialized by the real file — gog by the binary, xurl by
#: ``_locked_update``'s ``fcntl`` — but here the "file" is an S3 object with no such guard, so
#: two concurrent connects for one owner could each seed the same state and the later upload
#: would drop the earlier entry.
# ponytail: in-process lock only. The backend runs 2-4 tasks (service-stack desiredCount 2 +
# autoscale), so two connects for one owner landing on DIFFERENT tasks are still unserialized.
# Upgrade path if that ever bites: the DynamoDB claim-lease the provisioning path already uses.
# Not built, because the window is one owner connecting two accounts of one provider within the
# same couple of seconds from two browser tabs.
_owner_locks: dict[str, threading.RLock] = {}
_owner_locks_guard = threading.Lock()


def _owner_lock(owner_id: str) -> threading.RLock:
    # Reentrant: _run_and_sync merges cli-env.json (which takes it too) inside its own hold.
    with _owner_locks_guard:
        return _owner_locks.setdefault(owner_id, threading.RLock())


def _snapshot(root: Path, materializer: str) -> dict[str, bytes]:
    """Every file currently under this materializer's owned paths, keyed by its path relative to
    the PROFILE — i.e. its key suffix inside the profile prefix."""
    found: dict[str, bytes] = {}
    for owned in profile_paths(materializer):
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
    ``run_in_thread``, matching ``refresh_integrations``'s per-agent S3 convention.

    N5: the fan-out is sequential and bumps ``.revision`` per profile, so a mid-fanout failure
    leaves a partial roster. On a connect the caller's ``apply_failed`` rollback runs ``clear``,
    which converges it; on a disconnect the row is already gone and the next connect overwrites.
    """
    if not agent_ids:
        return None  # N2: an owner with no roster has nothing to project into.
    from core.services import hermes_profiles

    owned = profile_paths(materializer)
    fn = (MATERIALIZERS if write else MATERIALIZER_REMOVERS)[materializer]
    root_dir = ROOT_DIR.get(materializer, "")
    with _owner_lock(owner_id), tempfile.TemporaryDirectory(prefix="hermes-cli-") as tmp:
        root = Path(tmp)
        # Seed from the first profile that actually holds this provider's state: the backend writes
        # every profile identically, but a profile created after an earlier connect may not, and
        # seeding from an empty one would upload a keyring/.xurl without the earlier entries.
        # (None hold it: the scratch stays empty, a first connect.)
        for donor in agent_ids:
            hermes_profiles.fetch_profile_subtree(owner_id, donor, owned, root)
            if _snapshot(root, materializer):
                break
        before = set(_snapshot(root, materializer))
        with base.materialize_into(root / root_dir if root_dir else root):
            result = fn(owner_id, provider_key, credential)  # type: ignore[operator]
        files = _snapshot(root, materializer)
        gone = before - set(files)
        for agent_id in agent_ids:
            hermes_profiles.write_profile_subtree(owner_id, agent_id, files, gone)
        # cli-env.json is SHARED by every CLI provider (gog's keyring vars, each skill's token), so
        # it is merged key-by-key — writing it whole from one provider's keys wiped the others'
        # (reconnecting Google deleted VERCEL_TOKEN). Inside the same hold, so a concurrent
        # connect/disconnect cannot interleave between the owned files and their shell keys.
        shell_env = _shell_env(materializer, provider_key, credential) if write else {}
        updates = shell_env or ({} if write else dict.fromkeys(_shell_env_keys(materializer, provider_key)))
        for agent_id in agent_ids if updates else ():
            _merge_cli_env(owner_id, agent_id, updates)
    return result


def _merge_cli_env(owner_id: str, agent_id: str, updates: dict[str, str | None]) -> None:
    from core.services import hermes_profiles

    with _owner_lock(owner_id), tempfile.TemporaryDirectory(prefix="hermes-cli-env-") as tmp:
        root = Path(tmp)
        hermes_profiles.fetch_profile_subtree(owner_id, agent_id, (CLI_ENV_FILE,), root)
        try:
            env = json.loads((root / CLI_ENV_FILE).read_text())
        except (OSError, ValueError):
            env = {}  # Absent (nothing projected yet) or corrupt: the backend owns it, rebuild.
        if not isinstance(env, dict):
            env = {}
        for key, value in updates.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        if env:
            hermes_profiles.write_profile_subtree(owner_id, agent_id, {CLI_ENV_FILE: json.dumps(env).encode()}, ())
        else:  # the last key went (a full disconnect): no empty file left behind
            hermes_profiles.write_profile_subtree(owner_id, agent_id, {}, (CLI_ENV_FILE,))


def _seed_new_profile(owner_id: str, agent_id: str, donors: list[str]) -> None:
    """Sync twin of ``seed_new_profile``. Per materializer, copy the first donor's owned subtree
    that actually holds files; ``cli-env.json`` is copied minus its per-agent keys."""
    from core.services import hermes_profiles

    with _owner_lock(owner_id), tempfile.TemporaryDirectory(prefix="hermes-cli-seed-") as tmp:
        root = Path(tmp)
        files: dict[str, bytes] = {}
        for materializer in OWNED_PATHS:
            for donor in donors:
                hermes_profiles.fetch_profile_subtree(owner_id, donor, profile_paths(materializer), root)
                found = _snapshot(root, materializer)
                if found:
                    files.update(found)
                    break
        for donor in donors:
            hermes_profiles.fetch_profile_subtree(owner_id, donor, (CLI_ENV_FILE,), root)
            try:
                env = json.loads((root / CLI_ENV_FILE).read_text())
            except (OSError, ValueError):
                continue
            if isinstance(env, dict):
                env = {k: v for k, v in env.items() if k not in PER_AGENT_ENV_KEYS}
                if env:
                    files[CLI_ENV_FILE] = json.dumps(env).encode()
                break
        if files:
            hermes_profiles.write_profile_subtree(owner_id, agent_id, files, ())


async def seed_new_profile(owner_id: str, agent_id: str) -> None:
    """Give a profile created AFTER a connect the owner's connected CLI credentials. ``project``
    fans out over the roster as it was at connect time, so a hired / marketplace-deployed / created
    agent would otherwise read "connected" in the integrations row with no credential on disk.
    Copies from a sibling profile (credentials are identical across profiles by design -- see the
    module docstring). Best-effort: the profile already landed, so a failure is logged, not raised."""
    try:
        donors = [a for a in await _roster(owner_id) if a != agent_id]
        if donors:
            await run_in_thread(_seed_new_profile, owner_id, agent_id, donors)
    except Exception:  # noqa: BLE001
        logger.exception(
            "agentcore: seeding CLI credentials into new profile failed owner=%s agent=%s", owner_id, agent_id
        )


async def set_profile_cli_env(owner_id: str, agent_id: str, updates: dict[str, str | None]) -> None:
    """Merge backend-owned shell values into ONE agent's ``cli-env.json``; ``None`` deletes a key.

    Per-AGENT, unlike ``project()``'s roster fan-out — the caller is a per-agent binding
    (``google_identity``'s ``GOG_ACCOUNT``), i.e. exactly the values that must NOT be identical
    across profiles. Read-modify-write under the same owner lock the projection takes, so a
    concurrent connect cannot lose the merge, and ``.revision`` is bumped by
    ``write_profile_subtree`` so the bridge re-pulls the file on the agent's next turn
    (``cli-env.json`` is in the bridge's ``CLI_CREDENTIAL_PREFIXES``).

    A projection's connect/disconnect merges only its own keys into the same file (see
    ``_run_and_sync``), so per-agent keys set here survive it.
    """
    await run_in_thread(_merge_cli_env, owner_id, agent_id, updates)


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
