import json
import os
import stat
from unittest.mock import patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.credentials import hermes_cli  # noqa: E402
from core.services.credentials.materializers import MATERIALIZER_REMOVERS, MATERIALIZERS, shopify_cli  # noqa: E402

CRED = {"token": "mystore.myshopify.com|cid:shpss_se:cret"}


def test_write_seeds_skill_script_and_credentials(tmp_path):
    with patch.object(shopify_cli, "materializer_root", return_value=tmp_path):
        assert shopify_cli.write("org_1", "shopify", CRED) == "mystore.myshopify.com"

    d = tmp_path / "skills" / "shopify"
    # Split at the FIRST "|" and the FIRST ":" — a secret may contain either.
    assert json.loads((d / "credentials.json").read_text()) == {
        "shop": "mystore.myshopify.com",
        "client_id": "cid",
        "client_secret": "shpss_se:cret",
    }
    assert stat.S_IMODE((d / "credentials.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((d / "token.sh").stat().st_mode) == 0o700
    body = (d / "SKILL.md").read_text()
    assert "token.sh" in body and "graphql.json" in body


def test_remove_deletes_the_skill_dir(tmp_path):
    with patch.object(shopify_cli, "materializer_root", return_value=tmp_path):
        shopify_cli.write("org_1", "shopify", CRED)
        shopify_cli.remove("org_1", "shopify", CRED)
        shopify_cli.remove("org_1", "shopify", CRED)  # idempotent
    assert not (tmp_path / "skills" / "shopify").exists()


def test_registered_and_projected_into_the_profile_skills_dir():
    # Profile-relative like instagram: Hermes discovers skills by scanning <profile>/skills/.
    assert MATERIALIZERS["shopify_cli"] is shopify_cli.write
    assert MATERIALIZER_REMOVERS["shopify_cli"] is shopify_cli.remove
    assert hermes_cli.supports("shopify_cli")
    assert hermes_cli.profile_paths("shopify_cli") == ("skills/shopify/",)
