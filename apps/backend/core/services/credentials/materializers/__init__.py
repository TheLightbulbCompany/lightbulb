"""Materializer registry: name -> callable(owner_id, provider_key, credential).
Populated in M3 (toml_cli, gog_keyring)."""

MATERIALIZERS: dict[str, object] = {}
