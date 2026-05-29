"""Materializer registry: name -> callable(owner_id, provider_key, credential).

MATERIALIZERS write a CLI's credential file onto EFS; MATERIALIZER_REMOVERS
mirror them to clean up on disconnect."""

from core.services.credentials.materializers.gog_keyring import remove as _gog_remove
from core.services.credentials.materializers.gog_keyring import write as _gog
from core.services.credentials.materializers.toml_cli import remove as _toml_remove
from core.services.credentials.materializers.toml_cli import write as _toml

MATERIALIZERS: dict[str, object] = {"toml_cli": _toml, "gog_keyring": _gog}
MATERIALIZER_REMOVERS: dict[str, object] = {"toml_cli": _toml_remove, "gog_keyring": _gog_remove}
