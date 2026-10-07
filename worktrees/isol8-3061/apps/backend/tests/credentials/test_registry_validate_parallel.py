"""Parallel is a paste-token provider: an unvalidated paste let any string show
"Connected · Delivered". Pin that it validates against a real, authenticated endpoint."""

from core.services.credentials import registry


def test_parallel_validates_the_pasted_key_with_its_own_auth_header():
    v = registry.get_provider("parallel").validation
    assert v is not None
    assert (v.method, v.url, v.auth_header, v.auth_scheme) == (
        "POST",
        "https://api.parallel.ai/v1/search",
        "x-api-key",
        "",
    )
    assert v.body["search_queries"] and v.body["advanced_settings"]["max_results"] == 1
