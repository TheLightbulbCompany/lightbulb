"""Materializer registry: name -> callable(owner_id, provider_key, credential).

MATERIALIZERS write a CLI's credential file onto EFS; MATERIALIZER_REMOVERS
mirror them to clean up on disconnect."""

from core.services.credentials.materializers.aws_cli import remove as _aws_remove
from core.services.credentials.materializers.aws_cli import write as _aws
from core.services.credentials.materializers.gh_cli import remove as _gh_remove
from core.services.credentials.materializers.gh_cli import write as _gh
from core.services.credentials.materializers.gog_cli import remove as _gog_remove
from core.services.credentials.materializers.gog_cli import write as _gog
from core.services.credentials.materializers.instagram_cli import remove as _instagram_remove
from core.services.credentials.materializers.instagram_cli import write as _instagram
from core.services.credentials.materializers.mcp_oauth import remove as _mcp_oauth_remove
from core.services.credentials.materializers.mcp_oauth import write as _mcp_oauth
from core.services.credentials.materializers.skill_cli import remove as _skill_cli_remove
from core.services.credentials.materializers.skill_cli import write as _skill_cli
from core.services.credentials.materializers.toml_cli import remove as _toml_remove
from core.services.credentials.materializers.toml_cli import write as _toml

MATERIALIZERS: dict[str, object] = {
    "toml_cli": _toml,
    "gog_cli": _gog,
    "gh_cli": _gh,
    "instagram_cli": _instagram,
    "mcp_oauth": _mcp_oauth,
    "skill_cli": _skill_cli,
    "aws_cli": _aws,
}
MATERIALIZER_REMOVERS: dict[str, object] = {
    "toml_cli": _toml_remove,
    "gog_cli": _gog_remove,
    "gh_cli": _gh_remove,
    "instagram_cli": _instagram_remove,
    "mcp_oauth": _mcp_oauth_remove,
    "skill_cli": _skill_cli_remove,
    "aws_cli": _aws_remove,
}
