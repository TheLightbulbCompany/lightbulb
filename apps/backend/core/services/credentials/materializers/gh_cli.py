"""Materializer for GitHub via the `gh` CLI (baked into the OpenClaw image, not the
backend image).

`gh` is NOT installed in the backend, so — unlike gog (whose keyring only the real
binary can produce) — we cannot shell out to `gh auth login`. Instead we hand-write
gh's own `hosts.yml`, which is plain YAML gh reads verbatim at runtime. gh resolves
its auth from `$GH_CONFIG_DIR/hosts.yml`, else `$XDG_CONFIG_HOME/gh/hosts.yml`, else
`~/.config/gh/hosts.yml`. The container sets `XDG_CONFIG_HOME=/home/node/.openclaw/.config`,
which is the per-user EFS access point — so writing to `materializer_root/.config/gh/hosts.yml`
lands exactly where the container's `gh` looks (the same EFS mapping gog relies on).

On-disk, `hosts.yml`'s top level is the hostname directly (no `hosts:` wrapper — gh
re-nests it under `hosts:` only in its in-memory merged config). The key names
(`oauth_token`, `user`, `git_protocol`, `users`) match gh's config constants, and the
per-user sub-map mirrors the top-level token so gh's multi-user token lookup resolves.

The token is whatever the acquisition rail produced — an owner-pasted PAT or the
platform OAuth app's minted token; the username is fetched from the GitHub `/user`
endpoint with it (gh stamps `user:` into hosts.yml, and a wrong value breaks
`gh auth status` / account-scoped lookups).

Reprovision: the gh auth lives on EFS, so a config-rewrite reprovision (which only
touches openclaw.json) leaves it intact. A full EFS wipe drops it and needs a
re-connect. (`refresh_owner: none` keeps the PAT in Secrets Manager, so a future
reconcile-for-materializer pass could re-materialize it without the user re-pasting —
not wired today.)"""

from __future__ import annotations

import httpx
import yaml

from core.services.credentials.materializers.base import atomic_write, materializer_root

# The PAT has no embedded username, so resolve the GitHub login from the same endpoint
# the registry validates against. The `login` field is the account's username.
_GITHUB_USER_URL = "https://api.github.com/user"


def _hosts_path(owner_id: str):
    return materializer_root() / ".config" / "gh" / "hosts.yml"


def _fetch_login(token: str) -> str:
    resp = httpx.get(
        _GITHUB_USER_URL,
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()["login"]


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    # Paste rail delivers {"token": ...}; the direct_oauth rail delivers the
    # exchange payload keyed "access_token". Same shape to gh either way.
    token = credential.get("token") or credential["access_token"]
    login = _fetch_login(token)

    hosts = {
        "github.com": {
            "oauth_token": token,
            "user": login,
            "git_protocol": "https",
            "users": {login: {"oauth_token": token}},
        }
    }
    # default_flow_style=False emits the block form gh expects; atomic_write keeps the
    # 0600 + uid/gid 1000 posture shared by the whole materializer rail.
    body = yaml.safe_dump(hosts, default_flow_style=False, sort_keys=False)
    atomic_write(_hosts_path(owner_id), body.encode())


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    _hosts_path(owner_id).unlink(missing_ok=True)
