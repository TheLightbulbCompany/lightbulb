"""Shared hypothesis strategies for chaos machines."""

from hypothesis import strategies as st

# Identifiers — narrow alphabet keeps shrinking readable and DDB keys valid.
owner_ids = st.builds(lambda n: f"org_chaos_{n}", st.integers(min_value=0, max_value=5))
user_ids = st.builds(lambda n: f"user_chaos_{n}", st.integers(min_value=0, max_value=5))
emails = st.builds(lambda n: f"chaos{n}@example.com", st.integers(min_value=0, max_value=5))
listing_slugs = st.builds(lambda n: f"listing-{n}", st.integers(min_value=0, max_value=5))

# Token usage events for the usage-counter property test. Keys mirror
# usage_service.record_usage kwargs.
token_events = st.fixed_dictionaries(
    {
        "input_tokens": st.integers(min_value=0, max_value=100_000),
        "output_tokens": st.integers(min_value=0, max_value=100_000),
        "cache_read": st.integers(min_value=0, max_value=100_000),
        "cache_write": st.integers(min_value=0, max_value=100_000),
        "model": st.sampled_from(
            [
                "us.anthropic.claude-opus-4-8",
                "us.anthropic.claude-sonnet-4-6",
                "openai/gpt-4o",
            ]
        ),
    }
)
