"""Token custody ops (spec 13 §6): move a workspace's rotating grants into the backend's custody
and back, and report where they are.

    uv run python -m scripts.token_custody --owner-id ORG --show
    uv run python -m scripts.token_custody --owner-id ORG --measure-ttl
    uv run python -m scripts.token_custody --owner-id ORG --cutover codex|x|quickbooks|instagram|all [--apply]
    uv run python -m scripts.token_custody --owner-id ORG --rollback codex|x|quickbooks|instagram|all [--apply]
    uv run python -m scripts.token_custody --all --list-eligible

DRY RUN BY DEFAULT (every super-agent ops script): ``--cutover``/``--rollback`` only report what they
would do until ``--apply``. Dev-only unless ``--founder-approved REF`` (spec 09 pattern: the message
link or date+quote of the approval, printed with every decision). Prints decisions, ids and integer
seconds; never a token value. Idempotent: a second cutover answers ``already``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from core.config import settings
from core.repositories import billing_repo, integrations_repo
from core.services import oauth_service, token_custody
from core.services.container_provision import provider_choice_from_row

CLI_PROVIDERS = ("x", "quickbooks", "instagram")
TARGETS = ("codex", *CLI_PROVIDERS)


async def _codex_row(owner_id: str) -> dict | None:
    user = await oauth_service.resolve_token_owner(owner_id=owner_id)
    return await oauth_service.get_token_row(user, consistent=True, bound_to=owner_id)


async def _show(owner_id: str) -> None:
    choice, _ = await token_custody.resolve_provider_choice(owner_id)
    row = await _codex_row(owner_id)
    if row is None:
        print(f"codex: provider={choice} no_grant")
    else:
        expires_in = max(0, int(row.get("expires_at") or 0) // 1000 - int(time.time())) if row.get("expires_at") else -1
        print(
            f"codex: provider={choice} custody={row.get('custody') or 'actor'} version={int(row.get('version') or 0)} "
            f"state={row.get('state')} expires_in_s={expires_in}"
        )
    for r in await integrations_repo.list_for_owner(owner_id, consistent=True):
        if r.get("provider") in CLI_PROVIDERS:
            print(f"{r['provider']}: status={r.get('status', 'connected')} custody={r.get('custody') or 'actor'}")


async def _measure_ttl(owner_id: str) -> None:
    row = await _codex_row(owner_id)
    if row is None:
        print("codex: no_grant")
        return
    claims = oauth_service._jwt_claims(row["tokens"].get("access_token") or "")
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, int | float):
        print("codex: ttl_seconds=unknown")
        return
    ttl = int(exp - iat) if isinstance(iat, int | float) else int(exp - time.time())
    print(f"codex: ttl_seconds={ttl}")


async def _cli(action: str, owner_id: str, provider: str, *, dry_run: bool) -> str:
    if dry_run:
        # The same preconditions cli_cutover/cli_rollback check first, from the row, nothing written.
        row = await integrations_repo.get(owner_id, provider, consistent=True)
        connected = bool(row) and row.get("status", "connected") == "connected"
        backend = bool(row) and row.get("custody") == "backend"
        if action == "cutover":
            return "no_grant" if not connected else "already" if backend else "would_cutover"
        return "would_rollback" if backend else "already"
    try:
        from core.services.credentials import cli_cutover
    except ImportError:  # V14 not merged on this deploy
        return "unavailable_until_v14"
    fn = cli_cutover.cli_cutover if action == "cutover" else cli_cutover.cli_rollback
    return await fn(owner_id, provider)


async def _move(action: str, owner_id: str, target: str, *, dry_run: bool) -> int:
    failed = 0
    for provider in TARGETS if target == "all" else (target,):
        try:
            if provider == "codex":
                if action == "cutover":
                    result = await token_custody.codex_cutover(owner_id, dry_run=dry_run)
                else:
                    result = await token_custody.codex_rollback(owner_id, dry_run=dry_run)
            else:
                result = await _cli(action, owner_id, provider, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 -- one provider's failure must not stop the others
            failed += 1
            print(f"{provider}: ERROR {type(exc).__name__}")
            continue
        print(f"{provider}: {result}")
    return 1 if failed else 0


async def _list_eligible() -> None:
    """Owners with a rotating grant still in the VM's custody."""
    for billing in await billing_repo.scan_all():
        owner_id = billing.get("owner_id")
        if not owner_id or provider_choice_from_row(billing)[0] != "chatgpt_oauth":
            continue
        try:
            row = await _codex_row(owner_id)
        except Exception as exc:  # noqa: BLE001
            print(f"{owner_id}: codex ERROR {type(exc).__name__}")
            continue
        if row is not None and row.get("custody") != "backend":
            print(f"{owner_id}: codex")
    for r in await integrations_repo.scan_all():
        if (
            r.get("provider") in CLI_PROVIDERS
            and r.get("status", "connected") == "connected"
            and r.get("custody") != "backend"
        ):
            print(f"{r['owner_id']}: {r['provider']}")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--owner-id")
    who.add_argument("--all", action="store_true", help="every owner (only with --list-eligible)")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--cutover", choices=(*TARGETS, "all"))
    what.add_argument("--rollback", choices=(*TARGETS, "all"))
    what.add_argument("--show", action="store_true")
    what.add_argument("--list-eligible", action="store_true")
    what.add_argument("--measure-ttl", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write; without it --cutover/--rollback only report")
    parser.add_argument(
        "--founder-approved", metavar="REF", help="prod only: the approval (message link, or date + quote)"
    )
    args = parser.parse_args(argv)
    if settings.ENVIRONMENT == "prod" and not args.founder_approved:
        print("refusing: token_custody in prod needs --founder-approved REF")
        return 2
    if args.founder_approved:
        print(f"founder_approved={args.founder_approved}")
    if args.list_eligible:
        await _list_eligible()
        return 0
    if not args.owner_id:
        print("refusing: --all is only for --list-eligible")
        return 2
    if args.show:
        await _show(args.owner_id)
        return 0
    if args.measure_ttl:
        await _measure_ttl(args.owner_id)
        return 0
    dry_run = not args.apply
    if args.cutover:
        return await _move("cutover", args.owner_id, args.cutover, dry_run=dry_run)
    return await _move("rollback", args.owner_id, args.rollback, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
