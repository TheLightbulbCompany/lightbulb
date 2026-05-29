"""Materializer for Printing-Press agent-native CLIs, which read a 0600 TOML at
~/.config/<cli>/config.toml (access_token / refresh_token / token_expiry /
client_id / client_secret); the CLI self-refreshes from there."""

from __future__ import annotations

from core.services.credentials.materializers.base import atomic_write, owner_efs_root


def _toml_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    cli = credential["cli"]  # e.g. "outlook-email-pp-cli"
    path = owner_efs_root(owner_id) / ".config" / cli / "config.toml"
    fields = ["access_token", "refresh_token", "token_expiry", "client_id", "client_secret"]
    body = "\n".join(f'{k} = "{_toml_escape(str(credential[k]))}"' for k in fields if credential.get(k)) + "\n"
    atomic_write(path, body.encode())
