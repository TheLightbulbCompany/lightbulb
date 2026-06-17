import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import catalog_service, marketplace_service  # noqa: E402

_MANIFEST = {
    "requirements": [
        {"key": "stripe", "disposition": "connect", "kind": "external_api"},
        {"key": "slack", "disposition": "connect", "kind": "channel"},
        {"key": "plaid", "disposition": "byo", "kind": "external_api"},
        {"key": "gog", "disposition": "unavailable", "kind": "cli"},
        {"key": "llm-task", "disposition": "included", "kind": "bundled"},
    ]
}


def test_summarize_requirements_counts_and_providers():
    s = marketplace_service.summarize_requirements(_MANIFEST)
    assert s["counts"] == {"included": 1, "connect": 2, "byo": 1, "unavailable": 1}
    # channel connect (slack) excluded from connectable providers; only stripe
    assert s["requires_providers"] == ["stripe"]
    assert s["has_blocking"] is True


def test_summarize_requirements_no_blocking_when_no_unavailable():
    s = marketplace_service.summarize_requirements({"requirements": [{"key": "x", "disposition": "byo"}]})
    assert s["has_blocking"] is False


def test_summarize_requirements_handles_empty():
    assert marketplace_service.summarize_requirements({})["has_blocking"] is False
    assert marketplace_service.summarize_requirements(None)["counts"]["connect"] == 0


def test_warn_rows_are_byo_and_unavailable_only():
    warn = catalog_service.warn_rows(_MANIFEST)
    assert {w["key"] for w in warn} == {"plaid", "gog"}


def test_warn_rows_empty_manifest():
    assert catalog_service.warn_rows({}) == []
    assert catalog_service.warn_rows(None) == []
