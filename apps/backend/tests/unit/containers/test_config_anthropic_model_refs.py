"""BYO-Anthropic model refs must exist in the image's provider catalog.

OpenClaw 8.1 dropped `claude-sonnet-4-6` and `claude-opus-4-6` from
extensions/anthropic/openclaw.plugin.json. Both were present at 2026.7.2 and we
pinned them there; the 8.1 base bump removed them under us.

A ref the static catalog no longer resolves depends entirely on the plugin's
live discovery refresh against the owner's key. Before that lands — cold
container, first run, key or network failure — model-setup throws
`model_not_found`: the primary silently degrades to the haiku fallback, and
Friday's CEO pin has no per-agent fallback at all.

This is the same correction already applied on the OpenAI side (gpt-5.5 ->
gpt-5.6-sol) and never carried across.
"""

from core.containers.config import build_openclaw_config_dict

# Present in extensions/anthropic/openclaw.plugin.json at v2026.8.1-isol8.3.
_CATALOG_8_1 = {
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-8",
    "claude-haiku-4-5",
}

# In the 7.2 catalog, gone at 8.1.
_REMOVED_AT_8_1 = {"claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-7"}

_AIPS = {
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
}


# A seeded Friday. The CEO's premium pin is only emitted when a ceo-role agent
# is in the roster, so a config built without one never exercises it — which is
# exactly how claude-opus-4-6 stayed invisible.
_FRIDAY = {"id": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "name": "Friday", "role": "ceo"}


def _byo_anthropic_config(**kwargs) -> dict:
    defaults = {
        "user_id": "u_1",
        "gateway_token": "t" * 32,
        "provider_choice": "byo_key",
        "byo_provider": "anthropic",
        "aip_arns": _AIPS,
        "paperclip_agents": [_FRIDAY],
    }
    defaults.update(kwargs)
    return build_openclaw_config_dict(**defaults)


def _anthropic_refs(config: dict) -> set[str]:
    """Every `anthropic/<id>` ref anywhere in the emitted config."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, str):
            if node.startswith("anthropic/"):
                model = node.split("/", 1)[1]
                # "anthropic/*" is a model-policy allowlist wildcard, not a pin.
                if model != "*":
                    found.add(model)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(config)
    return found


def test_no_model_removed_at_8_1_is_emitted():
    refs = _anthropic_refs(_byo_anthropic_config())
    stale = refs & _REMOVED_AT_8_1
    assert not stale, (
        f"emitting {sorted(stale)}, which 8.1 removed from the Anthropic catalog; "
        "these resolve only if live discovery has already refreshed"
    )


def test_every_emitted_anthropic_ref_is_in_the_8_1_catalog():
    refs = _anthropic_refs(_byo_anthropic_config())
    assert refs, "expected at least one anthropic/ ref on the BYO-anthropic path"
    unknown = refs - _CATALOG_8_1
    assert not unknown, f"unknown to the 8.1 catalog: {sorted(unknown)}"


def test_the_ceo_pin_is_a_real_model():
    """Friday's premium pin has NO per-agent fallback, so a dead ref here does
    not degrade — it fails the run outright."""
    config = _byo_anthropic_config()
    refs = _anthropic_refs(config)
    # The CEO pin only appears when a ceo-role agent is in the roster; assert
    # the fixture actually produced one rather than silently testing nothing.
    roster = config["agents"].get("entries") or config["agents"].get("list") or {}
    assert roster, "fixture produced no agent roster — the CEO pin is untested"
    for ref in refs:
        assert ref in _CATALOG_8_1, f"CEO or primary pinned to a dead ref: {ref}"


def test_the_fallback_is_still_a_real_model():
    """The fallback is what a failed primary degrades TO — it must resolve."""
    config = _byo_anthropic_config()
    fallbacks = config["agents"]["defaults"]["model"]["fallbacks"]
    for ref in fallbacks:
        assert ref.split("/", 1)[1] in _CATALOG_8_1, f"dead fallback: {ref}"
