"""Materializer for the Instagram integration (platform Meta app, curl-based skill).

``write`` validates the freshly-exchanged token against the Graph API (also
resolving the account's username for the identity line in Settings), then:
  (a) seeds ``skills/instagram/SKILL.md`` into the profile — the agent-facing
      instructions for publish/comments/DMs via ``curl``;
  (b) writes ``skills/instagram/credentials.json`` (0600) next to it — the
      long-lived IG token + account ids the SKILL.md tells the agent to read.
      File-based like ``gh_cli``'s hosts.yml: the token is the owner's own
      asset-scoped grant, never the platform app secret (the dance blanks that
      before any materializer runs);
  (c) seeds ``skills/instagram/token.sh`` (0700 — it must be executable, unlike
      every other materialized artifact), which renews the 60-day token in place
      whenever the agent reads it. Instagram's refresh needs only the token
      itself, so renewal belongs in the container next to the one thing that
      uses it — there is no backend sweep and no scheduler.

Hermes finds a skill by SCANNING the profile, so there is no config entry to enable:
landing the three files above is the whole install. ``remove`` deletes the seeded dir.
Re-connects simply call ``write`` again — every artifact is replaced atomically.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx

from core.services.credentials.materializers.base import atomic_write, materializer_root

_SKILL_KEY = "instagram"
_SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / _SKILL_KEY
_SKILL_TEMPLATE = _SKILL_DIR / "SKILL.md"
_TOKEN_SCRIPT = _SKILL_DIR / "token.sh"
_GRAPH_ME_URL = "https://graph.instagram.com/v23.0/me"


def _skill_dir(owner_id: str) -> Path:
    return materializer_root() / "skills" / _SKILL_KEY


def _fetch_identity(token: str) -> dict:
    # Bearer header, not ?access_token= — Graph accepts either, and a token in the
    # query string ends up in every log of the request URL (httpx's own INFO line,
    # any intermediary's access log). Same posture as gog_cli's profile fetch.
    resp = httpx.get(
        _GRAPH_ME_URL,
        params={"fields": "user_id,username"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def write(owner_id: str, provider_key: str, credential: dict) -> str:
    token = credential["access_token"]
    me = _fetch_identity(token)
    username = me.get("username", "")

    body = _SKILL_TEMPLATE.read_text()
    atomic_write(_skill_dir(owner_id) / "SKILL.md", body.encode())
    # 0700, not the 0600 default: the agent EXECUTES this one. Written before
    # credentials.json so the SKILL.md's documented read path is never live
    # against a missing script.
    atomic_write(_skill_dir(owner_id) / "token.sh", _TOKEN_SCRIPT.read_bytes(), mode=0o700)

    brokered = credential.get("custody") == "backend"
    creds = {
        # Spec 13: under backend custody the bridge fills the vended token in locally each turn;
        # token.sh then only prints it (the backend renews within its 30-day window).
        "access_token": "" if brokered else token,
        # Prefer the Graph-reported id; fall back to the exchange's app-scoped id.
        "user_id": str(me.get("user_id") or credential.get("provider_user_id") or ""),
        "username": username,
        "token_expiry": "" if brokered else credential.get("token_expiry", ""),
    }
    if brokered:
        creds["custody"] = "backend"
    atomic_write(_skill_dir(owner_id) / "credentials.json", json.dumps(creds, indent=2).encode())
    return username


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    shutil.rmtree(_skill_dir(owner_id), ignore_errors=True)
