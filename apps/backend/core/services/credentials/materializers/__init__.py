"""Materializer registry: name -> callable(owner_id, provider_key, credential)."""

from core.services.credentials.materializers.gog_keyring import write as _gog
from core.services.credentials.materializers.toml_cli import write as _toml

MATERIALIZERS: dict[str, object] = {"toml_cli": _toml, "gog_keyring": _gog}
