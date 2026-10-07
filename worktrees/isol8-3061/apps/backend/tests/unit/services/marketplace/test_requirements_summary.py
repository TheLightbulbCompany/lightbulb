import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import catalog_service  # noqa: E402

_MANIFEST = {
    "requirements": [
        {"key": "stripe", "disposition": "connect", "kind": "external_api"},
        {"key": "slack", "disposition": "connect", "kind": "channel"},
        {"key": "plaid", "disposition": "byo", "kind": "external_api"},
        {"key": "gog", "disposition": "unavailable", "kind": "cli"},
        {"key": "llm-task", "disposition": "included", "kind": "bundled"},
    ]
}


def test_warn_rows_are_byo_and_unavailable_only():
    warn = catalog_service.warn_rows(_MANIFEST)
    assert {w["key"] for w in warn} == {"plaid", "gog"}


def test_warn_rows_empty_manifest():
    assert catalog_service.warn_rows({}) == []
    assert catalog_service.warn_rows(None) == []
