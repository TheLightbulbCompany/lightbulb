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

# Config builder inputs for the brick test. Values are the real provider_choice
# branches in core/containers/config.py. `bedrock_trial` is the free-tier config
# MODE (one AIP tier + the self-run lockdown: cron off, no heartbeat) — it emits
# keys no other branch does, so it must be sampled here or nothing proves those
# keys can't crash-loop a container.
provider_choices = st.sampled_from(["bedrock_claude", "bedrock_trial", "chatgpt_oauth", "byo_key"])
byo_providers = st.sampled_from(["anthropic", "openai"])
owner_types = st.sampled_from(["personal", "org"])

# Paperclip roster shapes the config builder can be handed. `[]` is the
# unprovisioned container (one legacy "main" entry); the two- and three-agent
# rosters are what a real owner runs, and only those exercise the schema's
# multi-agent superRefine — every agent-id cross-reference (systemAgent,
# talk.agentId, heartbeat.agentId, bindings, hooks.allowedAgentIds) is checked
# against the roster, and a single-"main" config can never fail those checks.
paperclip_rosters = st.sampled_from(
    [
        [],
        [
            {"id": "agent_friday", "name": "Friday", "role": "ceo"},
            {"id": "agent_atlas", "name": "Atlas", "role": "general"},
        ],
        # CEO not first, to keep the default-first ordering honest.
        [
            {"id": "agent_tally", "name": "Tally", "role": "general"},
            {"id": "agent_pulse", "name": "Pulse", "role": "general"},
            {"id": "agent_friday", "name": "Friday", "role": "ceo"},
        ],
        # No CEO at all -- the first entry owns ambient work.
        [
            {"id": "agent_vera", "name": "Vera", "role": "general"},
            {"id": "agent_gauge", "name": "Gauge", "role": "general"},
        ],
    ]
)
