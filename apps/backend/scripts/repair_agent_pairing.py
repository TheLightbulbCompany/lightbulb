"""Repair agents created against a RUNNING container that never got paired.

Every create path is supposed to call ``ensure_operator_device_paired`` right
after ``pair_with_openclaw`` -- but that function is entirely best-effort:
every one of its sub-steps (paired.json merge, live ``device.pair.approve``,
Paperclip adapterConfig rebind, per-agent identity staging) is wrapped in
``except Exception: logger.warning(...)`` (see
``core/services/paperclip_openclaw_pairing.py:443-642``). It never raises and
never returns a status, so a caller-side hiccup, a busy container missing the
5s handshake window, or a router that forgot to call it at all all land the
same way: the agent is registered in OpenClaw's own agent list
(``agents.create``/``agents.update`` already succeeded) but its operator
device is not in the container's trust store, and/or its Paperclip
``adapterConfig`` still carries the wrong ``authToken`` and is missing
``agentId``/``claimedApiKeyPath``. The missing ``agentId`` is the dangerous
half: unset, ``chat.send`` carries no agent and OpenClaw falls back to the
``default: true`` agent -- the ``role="ceo"`` one, i.e. Friday -- so every
run of the broken agent executes AS Friday, in Friday's workspace, reading
Friday's claimed key (see ``core/services/paperclip_adapter_config.py:107``).

This script re-runs ``ensure_operator_device_paired`` against EXISTING agents
and then INDEPENDENTLY VERIFIES the repair -- never trusting "it returned" as
"it worked", because the primitive swallows every failure. Verification reads
three things back, all sourced fresh (never reused from before the repair):

1. The container's own device trust list (``device.pair.list``, admin-signed,
   fully read-only -- it creates no pending request) -- proves the device
   pairing landed in the gateway's live SQLite trust store, not just in the
   ``paired.json`` fallback file that a running 7.2 gateway never re-reads.
2. A fresh re-fetch of the Paperclip agent -- proves ``adapterConfig.agentId``,
   ``claimedApiKeyPath``, and ``authToken`` (== the container's gateway_token)
   actually landed in Paperclip, not just in the PATCH body the pairing
   function *tried* to send.
3. A direct EFS read of the file ``claimedApiKeyPath`` points at -- proves the
   identity-staging sub-step actually produced a usable credentials file, not
   just that the config string naming it is correct. This does NOT catch a
   pre-existing file left STALE by ``write_file_if_missing``'s write-once
   semantics (see ``_verify_staged_identity``'s docstring) -- an ``OK`` here
   means "well-formed file present," not "token guaranteed live."

Every sub-step of ``ensure_operator_device_paired`` is idempotent (see its
docstring), so running this against an already-healthy agent is a no-op, and
re-running it after a partial failure completes the rest.

Target selection: pass explicit ``--agent-id`` values, or omit it entirely to
audit + repair every ``openclaw_gateway`` agent of the owner whose
``adapterConfig`` is missing ``agentId``/``claimedApiKeyPath`` or whose
``authToken`` no longer matches the container's ``gateway_token``. Note that
rule is adapterConfig-only: an agent whose adapterConfig is already correct
but whose *device* was never approved into the trust store (an
``openclaw_gateway_pairing_required`` failure with no adapterConfig drift)
will NOT be auto-selected -- pass its ``--agent-id`` explicitly. The device
trust line printed for every processed agent (both dry-run and apply) makes
that case visible either way.

Run from INSIDE the backend ECS task -- the live gateway calls
(``device.pair.list``, and everything ``ensure_operator_device_paired`` does
internally) dial the container's gateway directly at ``ws://<task-ip>:18789``,
and the identity-file staging step writes to the EFS mount. Neither is
reachable from a laptop::

    CLUSTER=$(aws ecs list-clusters --profile isol8-admin --region us-east-1 \\
      --output text --query 'clusterArns' | tr '\\t' '\\n' | grep isol8-prod-container)
    BACKEND_SERVICE=$(aws ecs list-services --cluster "$CLUSTER" --profile isol8-admin \\
      --region us-east-1 --output text --query 'serviceArns' | tr '\\t' '\\n' | grep isol8-prod-service)
    TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$BACKEND_SERVICE" \\
      --profile isol8-admin --region us-east-1 --query 'taskArns[0]' --output text | awk -F/ '{print $NF}')

    # DRY RUN -- reads only: DDB, Cloud Map, Paperclip list, device.pair.list
    aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container backend \\
      --interactive --profile isol8-admin --region us-east-1 \\
      --command "/bin/sh -c 'cd /app && uv run python -m scripts.repair_agent_pairing \\
         --env prod --owner-id org_xxx --agent-id <uuid>,<uuid>,<uuid>'"

    # APPLY
    aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container backend \\
      --interactive --profile isol8-admin --region us-east-1 \\
      --command "/bin/sh -c 'cd /app && uv run python -m scripts.repair_agent_pairing \\
         --env prod --owner-id org_xxx --agent-id <uuid>,<uuid>,<uuid> --apply'"

Dry-run by default (reads only: no probe, no PATCH, no writes). Pass
``--apply`` to actually repair. This is a one-shot incident script: per this
repo's CLAUDE.md, delete it (and its test) once the incident is closed --
git history is the operational record.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

logger = logging.getLogger("repair_agent_pairing")


def _split_ids(raw: list[str] | None) -> list[str]:
    """Flatten repeated and/or comma-separated ids, first-seen order, no blanks."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk in raw or []:
        for part in chunk.split(","):
            v = part.strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


def _needs_repair(cfg: dict, *, desired: dict) -> list[str]:
    """The subset of ``desired`` keys whose current value in ``cfg`` disagrees.

    Shared by target auto-selection and by both the before/after verification
    prints, so all three read the exact same drift definition.
    """
    return sorted(k for k, v in desired.items() if cfg.get(k) != v)


async def main(*, env: str, owner_id: str, agent_ids: list[str], apply: bool) -> int:
    # Same rationale as scripts/reapply_openclaw_config.py: ENVIRONMENT picks
    # the paperclip-admin secret and the per-agent key secret path
    # (isol8/<env>/paperclip-agent-keys/...). Does not affect DDB/EFS
    # targeting, which is keyed on DYNAMODB_TABLE_PREFIX / the process env.
    os.environ["ENVIRONMENT"] = env

    # Imports inside main so module import stays cheap/side-effect-free and
    # ENVIRONMENT is set before anything reads core.config settings.
    import httpx

    from core.config import settings
    from core.containers import get_ecs_manager, get_workspace
    from core.containers.workspace import WorkspaceError
    from core.crypto.operator_device import device_identity_from_pem
    from core.gateway.operator_rpc import gateway_rpc_oneshot
    from core.repositories import container_repo
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_adapter_config import (
        OPENCLAW_GATEWAY_TYPE,
        claimed_api_key_path,
        desired_agent_adapter_fields,
    )
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_openclaw_pairing import (
        ensure_operator_device_paired,
        fetch_paperclip_agents_for_owner,
    )

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        print(f"[{env}] No container row for owner {owner_id} -- nothing to repair")
        return 1
    gateway_token = container.get("gateway_token")
    if not gateway_token:
        print(f"[{env}] Container row for {owner_id} has no gateway_token -- needs a full reprovision")
        return 1

    # Cloud Map, never describe_tasks / never cached -- the task IP is
    # ephemeral (deploy, OOM, reprovision all change it).
    _, ip = await get_ecs_manager().resolve_running_container(owner_id)
    if not ip:
        print(f"[{env}] {owner_id}: no reachable running task (Cloud Map found none) -- nothing to repair")
        return 1

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    workspace = get_workspace()

    def _verify_staged_identity(agent_id: str) -> str:
        """Read back the staged Paperclip identity file and classify it.

        The primitive's ``claimedApiKeyPath`` bookkeeping (checked by
        ``_needs_repair`` above) only proves the CONFIG STRING is right --
        not that the file it points at exists or has usable credentials.
        This reads the actual EFS artifact the container consumes, resolved
        the same way ``stage_paperclip_identity_for_agent`` writes it (see
        ``core/services/friday_identity.py``): ``claimed_api_key_path``
        returns the in-container path
        (``~/.openclaw/workspace-{id}/paperclip-claimed-api-key.json``); the
        backend's per-owner workspace root IS that same ``~/.openclaw`` dir
        (see ``container-stack.ts`` HOME/EFS mount), so stripping the prefix
        gives the workspace-relative path ``Workspace.read_file`` expects.

        Returns "ok", "missing" (no such file), or "incomplete" (file exists
        but isn't parseable JSON, is missing a required field, or names the
        wrong agent -- never invents a schema beyond what the staging code
        writes: ``token`` / ``agentId`` / ``companyId`` / ``apiUrl``).

        Does NOT detect a STALE pre-existing token: ``write_file_if_missing``
        never overwrites a file that already exists, so a file staged before
        a key rotation reads back "ok" here even though its token may be
        dead. Distinguishing that cheaply would need an extra Secrets
        Manager round trip per agent to fetch the current canonical token;
        this script does not attempt it -- treat "ok" as "the file is
        present and well-formed," not "the token is guaranteed live."
        """
        rel_path = claimed_api_key_path(agent_id).removeprefix("~/.openclaw/")
        try:
            raw = workspace.read_file(owner_id, rel_path)
        except WorkspaceError:
            return "missing"
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return "incomplete"
        if not isinstance(data, dict):
            return "incomplete"
        required = ("token", "agentId", "companyId", "apiUrl")
        if any(not isinstance(data.get(f), str) or not data.get(f) for f in required):
            return "incomplete"
        if data.get("agentId") != agent_id:
            return "incomplete"
        return "ok"

    async def _fetch_agents() -> dict[str, dict]:
        # Fresh, unredacted fetch every time this is called -- never reuse a
        # snapshot across the repair, or a stale `cfg` would clobber whatever
        # changed in Paperclip since (ensure_operator_device_paired PATCHes
        # {**cfg, **desired} with replaceAdapterConfig=True).
        agents = await fetch_paperclip_agents_for_owner(
            owner_id=owner_id,
            repo=PaperclipRepo(table_name="paperclip-companies"),
            admin_client=PaperclipAdminClient(http_client=http),
        )
        return {a["id"]: a for a in agents if isinstance(a, dict) and a.get("id")}

    async def _trusted_device_ids() -> set[str]:
        # Read-only: device.pair.list creates no pending pairing request
        # (unlike the connect-as-device probe), so it's safe to call in
        # dry-run and as often as we like.
        payload = await gateway_rpc_oneshot(
            ip, owner_id=owner_id, token=gateway_token, method="device.pair.list", params={}
        )
        return {
            str(d.get("deviceId", "")).strip()
            for d in (payload.get("paired") or [])
            if isinstance(d, dict) and d.get("deviceId")
        }

    try:
        by_id = await _fetch_agents()
        if not by_id:
            print(f"[{env}] {owner_id}: no Paperclip agents found -- nothing to repair")
            return 1

        if agent_ids:
            targets = agent_ids
        else:
            targets = [
                a["id"]
                for a in by_id.values()
                if a.get("adapterType") == OPENCLAW_GATEWAY_TYPE
                and _needs_repair(
                    a.get("adapterConfig") or {},
                    desired=desired_agent_adapter_fields(agent_id=a["id"], gateway_token=gateway_token),
                )
            ]

        print(f"[{env}] owner={owner_id} service={container.get('service_name')} ip={ip}")
        print(f"[{env}] paperclip agents={len(by_id)} targets={len(targets)} -- {'APPLY' if apply else 'DRY RUN'}")
        if not targets:
            print(f"[{env}] Nothing to repair -- every openclaw_gateway agent's adapterConfig is current")
            return 0

        trusted = await _trusted_device_ids()

        n_ok = n_skip = n_err = 0
        for aid in targets:
            agent = by_id.get(aid)
            if agent is None:
                n_err += 1
                print(f"  ERR  {aid}: not in this owner's Paperclip company")
                continue
            name = agent.get("name") or aid
            if agent.get("adapterType") != OPENCLAW_GATEWAY_TYPE:
                n_skip += 1
                print(f"  SKIP {aid} ({name}): adapterType={agent.get('adapterType')!r}, not openclaw_gateway")
                continue
            cfg = agent.get("adapterConfig") or {}
            pem = cfg.get("devicePrivateKeyPem")
            if not pem:
                # ensure_operator_device_paired no-ops on these too -- no
                # stable Isol8-minted device to pair, the agent must be
                # recreated.
                n_skip += 1
                print(f"  SKIP {aid} ({name}): no devicePrivateKeyPem -- recreate the agent")
                continue

            desired = desired_agent_adapter_fields(agent_id=aid, gateway_token=gateway_token)
            drift_before = _needs_repair(cfg, desired=desired)
            try:
                device_id = device_identity_from_pem(pem).device_id
            except Exception as e:  # noqa: BLE001 -- match ensure_operator_device_paired's own guard
                n_err += 1
                print(f"  ERR  {aid} ({name}): bad device PEM -- {e}")
                continue
            trusted_before = device_id in trusted
            # Never print token/PEM material -- key names + booleans only.
            print(
                f"  --   {aid} ({name}): device={device_id[:16]} trusted={trusted_before} "
                f"adapter_drift={drift_before or 'none'}"
            )

            if not apply:
                continue

            await ensure_operator_device_paired(
                owner_id=owner_id,
                paperclip_agent=agent,
                gateway_token=gateway_token,
                ip=ip,
            )

            # Independent verification -- re-read both channels fresh. The
            # primitive above never raises and never tells us whether it
            # worked; this is the actual proof.
            after_agents = await _fetch_agents()
            after_agent = after_agents.get(aid) or {}
            after_cfg = after_agent.get("adapterConfig") or {}
            drift_after = _needs_repair(after_cfg, desired=desired)
            trusted_after = device_id in (await _trusted_device_ids())
            identity_status = _verify_staged_identity(aid)

            if not drift_after and trusted_after and identity_status == "ok":
                n_ok += 1
                print(f"  OK   {aid} ({name}): device trusted + adapterConfig bound + identity file staged")
            else:
                n_err += 1
                identity_reason = {
                    "missing": "identity file missing",
                    "incomplete": "identity file present but incomplete",
                }.get(identity_status)
                extra = f" identity={identity_reason}" if identity_reason else ""
                print(
                    f"  ERR  {aid} ({name}): trusted={trusted_after} remaining_adapter_drift={drift_after or 'none'}"
                    f"{extra} -- see CloudWatch for the swallowed warning from ensure_operator_device_paired"
                )

        print(f"[{env}] Done: repaired={n_ok} skipped={n_skip} failed={n_err}")
        if not apply:
            print("DRY RUN -- pass --apply to repair")
        return 0 if n_err == 0 else 1
    finally:
        await http.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=["dev", "prod"], help="Target environment.")
    parser.add_argument("--owner-id", required=True, help="Container owner id (org_* or user_*).")
    parser.add_argument(
        "--agent-id",
        action="append",
        default=[],
        help="Paperclip agent UUID. Repeatable and/or comma-separated. "
        "Omit to auto-select every openclaw_gateway agent whose adapterConfig has drifted.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually repair (default: dry run).")
    args = parser.parse_args()
    sys.exit(
        asyncio.run(
            main(
                env=args.env,
                owner_id=args.owner_id,
                agent_ids=_split_ids(args.agent_id),
                apply=args.apply,
            )
        )
    )
