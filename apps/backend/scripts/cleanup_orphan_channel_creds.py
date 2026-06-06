"""One-shot cleanup: strip legacy provider-level channel credentials.

Removes ``botToken`` / ``appToken`` / ``token`` / ``signingSecret`` sitting
directly on ``channels.<provider>`` (NOT under ``accounts.<id>``) across the
fleet. Those are orphans from an older setup wizard that wrote tokens at the
provider level; OpenClaw reads them as a synthetic ``[default]`` account that
crash-loops ``auth.test`` / ``getMe`` on revoked creds. See
``docs/superpowers/research/2026-06-05-channels-audit-5.28.md``.

The delete-bot path now strips these going forward
(``delete_provider_level_channel_secrets``); this script cleans up containers
that already carry orphans.

Run via::

    cd apps/backend
    uv run python -m scripts.cleanup_orphan_channel_creds --env prod
    uv run python -m scripts.cleanup_orphan_channel_creds --env prod --apply

Defaults to dry-run; pass ``--apply`` to actually strip. Idempotent — leaves
real ``accounts.<id>`` tokens untouched, so re-running is safe.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger("cleanup_orphan_channel_creds")

_PROVIDERS = ("telegram", "discord", "slack")


def find_orphan_provider_secrets(
    config: Any,
    providers: tuple[str, ...] = _PROVIDERS,
) -> dict[str, list[str]]:
    """Return ``{provider: [secret_keys_present]}`` for provider-level
    credential keys sitting directly on ``channels.<provider>``.

    Tokens nested under ``accounts.<id>`` are legitimate and ignored.
    """
    # Lazy import: pulls in core.config (settings validation) which needs env
    # vars — keep it out of module scope so --help / unit collection work.
    from core.services.config_patcher import PROVIDER_LEVEL_CHANNEL_SECRET_KEYS

    out: dict[str, list[str]] = {}
    if not isinstance(config, dict):
        return out
    channels = config.get("channels")
    if not isinstance(channels, dict):
        return out
    for provider in providers:
        channel = channels.get(provider)
        if not isinstance(channel, dict):
            continue
        present = [k for k in PROVIDER_LEVEL_CHANNEL_SECRET_KEYS if k in channel]
        if present:
            out[provider] = present
    return out


async def _scan_owner_ids() -> list[str]:
    """Return every container ``owner_id`` (deduped, sorted)."""
    from core.repositories import container_repo

    rows = await container_repo.scan_all()
    return sorted({r["owner_id"] for r in rows if r.get("owner_id")})


async def main(*, apply: bool, env: str) -> int:
    # Imports inside main so script setup (path, env vars) happens first.
    from core.containers.config import read_openclaw_config_from_efs
    from core.services.config_patcher import delete_provider_level_channel_secrets

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    owner_ids = await _scan_owner_ids()
    affected_owners = 0
    total_providers = 0
    for owner_id in owner_ids:
        try:
            config = await read_openclaw_config_from_efs(owner_id)
        except Exception as e:  # noqa: BLE001
            print(f"  SKIP {owner_id}: cannot read config ({e})")
            continue
        orphans = find_orphan_provider_secrets(config or {})
        if not orphans:
            continue
        affected_owners += 1
        for provider, keys in sorted(orphans.items()):
            total_providers += 1
            print(f"  {owner_id} channels.{provider}: {', '.join(keys)}")
            if apply:
                try:
                    await delete_provider_level_channel_secrets(owner_id, provider)
                except Exception as e:  # noqa: BLE001
                    # Don't let one transient owner (e.g. container deleted
                    # mid-run) abort the whole fleet sweep.
                    print(f"  ERR  {owner_id} channels.{provider}: strip failed ({e})")

    print(f"[{env}] {affected_owners} owner(s), {total_providers} provider(s) with orphan provider-level creds")
    if not apply:
        print("DRY RUN — pass --apply to strip")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("Strip legacy provider-level channel credentials across the fleet. Defaults to dry-run."),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually strip the orphan creds (default: dry-run).",
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["dev", "prod"],
        help="Environment label (informational; doesn't change DDB targeting).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, env=args.env)))
