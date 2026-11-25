"""The HogQL filter the Spark metrics share (core.services.ops.growth)."""

from core.services.ops import growth as g


def test_hogql_reads_only_this_environment_and_on_prod_only_product_hosts():
    """T23 (09-analytics §6): dev events and the launch site never enter the admin metrics."""
    prod = g._hogql_filter("prod")
    assert "properties.environment = 'prod'" in prod
    assert "'thelightbulb.company', 'www.thelightbulb.company'" in prod
    assert g._hogql_filter("dev") == "properties.environment = 'dev'"
    assert g._hogql_filter("") == "properties.environment = 'local'"
