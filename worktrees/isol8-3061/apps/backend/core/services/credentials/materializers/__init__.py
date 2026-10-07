"""Materializer registry: name -> callable(owner_id, provider_key, credential).

MATERIALIZERS write a CLI's credential file into a scratch home that
``hermes_cli.project`` syncs into every Hermes profile; MATERIALIZER_REMOVERS
mirror them to clean up on disconnect. Only names in ``hermes_cli.OWNED_PATHS``
are ever dispatched."""

from core.services.credentials.materializers.aws_cli import remove as _aws_remove
from core.services.credentials.materializers.aws_cli import write as _aws
from core.services.credentials.materializers.gh_cli import remove as _gh_remove
from core.services.credentials.materializers.gh_cli import write as _gh
from core.services.credentials.materializers.gog_cli import remove as _gog_remove
from core.services.credentials.materializers.gog_cli import write as _gog
from core.services.credentials.materializers.instagram_cli import remove as _instagram_remove
from core.services.credentials.materializers.instagram_cli import write as _instagram
from core.services.credentials.materializers.qbo_dotenv import remove as _qbo_remove
from core.services.credentials.materializers.qbo_dotenv import write as _qbo
from core.services.credentials.materializers.shopify_cli import remove as _shopify_remove
from core.services.credentials.materializers.shopify_cli import write as _shopify
from core.services.credentials.materializers.skill_cli import remove as _skill_cli_remove
from core.services.credentials.materializers.skill_cli import write as _skill_cli
from core.services.credentials.materializers.xurl_cli import remove as _xurl_remove
from core.services.credentials.materializers.xurl_cli import write as _xurl

MATERIALIZERS: dict[str, object] = {
    "gog_cli": _gog,
    "gh_cli": _gh,
    "instagram_cli": _instagram,
    "skill_cli": _skill_cli,
    "shopify_cli": _shopify,
    "aws_cli": _aws,
    "qbo_dotenv": _qbo,
    "xurl_cli": _xurl,
}
MATERIALIZER_REMOVERS: dict[str, object] = {
    "gog_cli": _gog_remove,
    "gh_cli": _gh_remove,
    "instagram_cli": _instagram_remove,
    "skill_cli": _skill_cli_remove,
    "shopify_cli": _shopify_remove,
    "aws_cli": _aws_remove,
    "qbo_dotenv": _qbo_remove,
    "xurl_cli": _xurl_remove,
}
