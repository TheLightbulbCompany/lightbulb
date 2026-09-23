"""Materializer for the Shopify integration (Dev Dashboard client credentials, curl-based skill).

The stored credential is the joined paste ``"<shop>|<client_id>:<client_secret>"``. ``write``
lands three files in the profile's ``skills/shopify/`` — the same profile-relative shape as
``instagram_cli``:
  * ``SKILL.md`` — the agent-facing instructions (Admin GraphQL via curl + jq);
  * ``token.sh`` (0700) — mints a 24 h Admin API token with the client-credentials grant and
    caches it next to itself until ~5 min before expiry;
  * ``credentials.json`` (0600) — shop, client_id, client_secret for ``token.sh``.
A file rather than ``cli-env.json`` on purpose: that file is rewritten wholesale per connect
(it would drop another provider's keys) and the bridge only passes allowlisted names from it,
so an env-var delivery would also need an image rebuild. ``remove`` deletes the dir.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from core.services.credentials.materializers.base import atomic_write, materializer_root

_SKILL_SRC = Path(__file__).resolve().parents[1] / "skills" / "shopify"


def _skill_dir() -> Path:
    return materializer_root() / "skills" / "shopify"


def write(owner_id: str, provider_key: str, credential: dict) -> str:
    # FIRST separators, matching the validator's partition: a secret may contain ':' or '|'.
    shop, _, pair = credential["token"].partition("|")
    client_id, _, client_secret = pair.partition(":")
    dest = _skill_dir()
    atomic_write(dest / "SKILL.md", (_SKILL_SRC / "SKILL.md").read_bytes())
    atomic_write(dest / "token.sh", (_SKILL_SRC / "token.sh").read_bytes(), mode=0o700)
    creds = {"shop": shop, "client_id": client_id, "client_secret": client_secret}
    atomic_write(dest / "credentials.json", json.dumps(creds, indent=2).encode())
    return shop


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    shutil.rmtree(_skill_dir(), ignore_errors=True)
