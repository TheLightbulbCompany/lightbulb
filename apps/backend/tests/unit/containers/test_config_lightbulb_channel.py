import pytest

from core.containers.config import build_openclaw_config_dict

FRIDAY = "11111111-1111-4111-8111-111111111111"
GAUGE = "22222222-2222-4222-8222-222222222222"

# The extended build one pin BEFORE the channel plugin was baked in. Same
# upstream base and same isol8 patch level as the builds that carry it.
PRE_PLUGIN_TAG = "2026.9.3-isol8.2-449894a"


@pytest.fixture(autouse=True)
def _emit_lightbulb_channel():
    """Override the directory-wide fixture, which forces the channel on. This
    module exercises BOTH sides of the baked-plugin gate, the way production
    does — by passing the tag of the image the config will run on.
    """
    yield


def _config(agents, **kwargs):
    kwargs.setdefault("deployed_tag", "2026.9.3-isol8.2-test")
    return build_openclaw_config_dict(
        user_id="org_test",
        gateway_token="gateway-test",
        provider_choice="chatgpt_oauth",
        paperclip_agents=agents,
        **kwargs,
    )


def _agent(agent_id, name, role="general"):
    return {"id": agent_id, "name": name, "role": role}


def test_one_agent_emits_exact_lightbulb_channel_contract():
    cfg = _config([_agent(FRIDAY, "Friday", "ceo")])
    assert cfg["plugins"]["entries"]["lightbulb"] == {"enabled": True}
    assert cfg["channels"]["lightbulb"] == {"enabled": True, "accounts": {FRIDAY: {"enabled": True}}}
    assert cfg["bindings"] == [
        {
            "agentId": FRIDAY,
            "match": {
                "channel": "lightbulb",
                "accountId": FRIDAY,
                "peer": {"kind": "channel", "id": "*"},
            },
        }
    ]
    assert cfg["session"]["groupScope"] == "per-group"
    assert cfg["messages"]["groupChat"] == {
        "unmentionedInbound": "room_event",
        "visibleReplies": "automatic",
        "historyLimit": 50,
    }
    assert cfg["tools"]["sessions"]["visibility"] == "agent"
    assert cfg["tools"]["agentToAgent"]["enabled"] is False


def test_two_agents_are_sorted_deduplicated_and_route_to_equal_native_session_keys():
    cfg = _config([_agent(GAUGE, "Gauge"), _agent(FRIDAY, "Friday", "ceo"), _agent(GAUGE, "Gauge")])
    ids = list(cfg["channels"]["lightbulb"]["accounts"])
    assert ids == [FRIDAY, GAUGE]
    assert [binding["agentId"] for binding in cfg["bindings"]] == ids
    room_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    for binding in cfg["bindings"]:
        agent_id = binding["agentId"]
        inbound = f"agent:{agent_id}:lightbulb:channel:{room_id}"
        outbound = f"agent:{binding['match']['accountId']}:lightbulb:channel:{room_id}"
        assert inbound == outbound


def test_empty_roster_keeps_main_fallback_account():
    cfg = _config([])
    assert cfg["channels"]["lightbulb"]["accounts"] == {"main": {"enabled": True}}
    assert cfg["bindings"][0]["agentId"] == "main"


@pytest.mark.parametrize(
    "tag",
    [
        PRE_PLUGIN_TAG,  # same base + fork patch, one build too early
        "2026.8.1-isol8.3-4e22eb8",  # an older base still live in the prod fleet
        "",  # a container whose image could not be read
        None,  # no container row yet
    ],
)
def test_image_without_the_baked_plugin_gets_no_lightbulb_channel(tag):
    # An image that does not bake the plugin rejects the WHOLE config at boot
    # ("unknown channel id: lightbulb") and exits 1, so none of the three keys
    # may be written for it.
    cfg = _config([_agent(FRIDAY, "Friday", "ceo")], deployed_tag=tag)
    assert "lightbulb" not in cfg["plugins"]["entries"]
    assert "lightbulb" not in cfg.get("channels", {})
    assert cfg.get("bindings", []) == []


def test_first_image_that_bakes_the_plugin_emits_the_channel():
    cfg = _config([_agent(FRIDAY, "Friday", "ceo")], deployed_tag="2026.9.3-isol8.2-fa8bf22")
    assert cfg["plugins"]["entries"]["lightbulb"] == {"enabled": True}
    assert cfg["channels"]["lightbulb"]["accounts"] == {FRIDAY: {"enabled": True}}
    assert [b["agentId"] for b in cfg["bindings"]] == [FRIDAY]


def test_gated_off_still_carries_configured_third_party_channels():
    # The carry-forward block setdefault()s `channels`/`bindings`, so dropping
    # the lightbulb literal must not take an owner's Slack config with it.
    slack_binding = {"type": "route", "agentId": FRIDAY, "match": {"channel": "slack", "accountId": "team-1"}}
    cfg = _config(
        [_agent(FRIDAY, "Friday", "ceo")],
        deployed_tag=PRE_PLUGIN_TAG,
        channel_accounts={"slack": {"team-1": {"botToken": "test-only"}}},
        channel_bindings=[slack_binding],
    )
    assert cfg["channels"]["slack"]["accounts"] == {"team-1": {"botToken": "test-only"}}
    assert slack_binding in cfg["bindings"]
    assert "lightbulb" not in cfg["channels"]


def test_lightbulb_preserves_configured_slack_account_and_binding():
    slack_binding = {"type": "route", "agentId": FRIDAY, "match": {"channel": "slack", "accountId": "team-1"}}
    cfg = _config(
        [_agent(FRIDAY, "Friday", "ceo")],
        channel_accounts={"slack": {"team-1": {"botToken": "test-only"}}},
        channel_bindings=[slack_binding],
    )
    assert cfg["channels"]["slack"]["accounts"] == {"team-1": {"botToken": "test-only"}}
    assert slack_binding in cfg["bindings"]
    assert len([binding for binding in cfg["bindings"] if binding["match"]["channel"] == "lightbulb"]) == 1
