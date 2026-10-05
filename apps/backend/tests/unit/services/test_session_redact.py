"""session_redact: what an outsider's copy or event page never shows (spec §3.4)."""

import pytest

from core.services.session_redact import MARK, PROJECT, redact, redact_value


@pytest.mark.parametrize(
    "secret",
    [
        'api_key = "abcdefghijklmnop1234"',  # marketplace hardcoded-secret
        "AKIAABCDEFGHIJKLMNOP",  # aws-secret-key
        "sk-ant-api03-abcdefghijklmnopqrstuvwx",  # provider-token
        "ghp_abcdefghijklmnopqrstuvwx",
        "sk_live_abcdefghijklmnop",  # Stripe
        "rk_test_abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",  # JWT
        "AIzaSyA-abcdefghijklmnopqrstuvwxyz12345",  # Google
    ],
)
def test_each_secret_shape_is_replaced(secret):
    out = redact(f"before {secret} after")
    assert secret not in out and MARK in out and out.startswith("before ")


def test_pem_block_env_line_bearer_and_url_credentials():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    assert redact(f"x\n{pem}\ny") == f"x\n{MARK}\ny"
    assert redact("export DATABASE_PASSWORD=hunter2") == f"export DATABASE_PASSWORD={MARK}"
    assert redact("STRIPE_SECRET_KEY = abc") == f"STRIPE_SECRET_KEY = {MARK}"
    assert redact("Authorization: Bearer abc.def") == f"Authorization: Bearer {MARK}"
    assert redact("git clone https://bob:pa55@github.com/o/r") == f"git clone https://{MARK}@github.com/o/r"


def test_home_directories_become_tilde():
    assert redact("/Users/alice/code/app and /home/bob/x") == "~/code/app and ~/x"


def test_plain_text_is_untouched():
    text = "Ran 12 tests in apps/backend; the token budget is 4000; KEY points to the map."
    assert redact(text) == text


def test_redact_value_walks_nested_structures_but_not_keys():
    value = {"cwd": "/Users/alice/app", "tool": {"input": ["ghp_abcdefghijklmnopqrstuvwx", 3, None]}, "n": 1}
    assert redact_value(value) == {"cwd": "~/app", "tool": {"input": [MARK, 3, None]}, "n": 1}


def test_a_private_projects_folder_name_is_hidden_only_as_a_path_component():
    line = {
        "cwd": "/Users/alice/code/acme-api/src",
        "text": "Read ./acme-api/README.md and acme-api.rs; acme-api is fine",
    }
    assert redact_value(line, "acme-api") == {
        "cwd": f"~/code/{PROJECT}/src",
        "text": f"Read ./{PROJECT}/README.md and acme-api.rs; acme-api is fine",
    }
    assert redact("/home/bob/acme-api", "acme-api") == f"~/{PROJECT}"
    assert redact("/srv/acme-api.v2/x", "acme-api.v2") == f"/srv/{PROJECT}/x"  # the name is literal, not a regex
    assert redact("/Users/alice/acme-api") == "~/acme-api"  # no project: unchanged
