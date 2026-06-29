"""Regression guard: emitted openclaw.json keeps exec on the gateway host.

The Vercel CLI integration (and any other CLI-token integration) injects its
token into the agent's exec environment as ``VERCEL_TOKEN``. OpenClaw only
merges skill/integration-injected env vars into the exec environment when the
exec runs on the **gateway** host — NOT when it runs inside a sandbox. Our
config achieves the gateway host *implicitly*, via two properties of the
emitted ``openclaw.json``:

  1. ``agents.defaults.sandbox`` is never set, so no per-agent sandbox is
     configured, and
  2. ``tools.exec.host`` is never pinned (it stays the default ``auto``),
     which resolves to ``gateway`` precisely because no sandbox exists.

If a future change adds ``agents.defaults.sandbox`` or pins ``tools.exec.host``
to something other than ``gateway``/``auto``, exec would resolve to a sandbox
host and silently drop every CLI-injected env var (VERCEL_TOKEN and friends) —
breaking the integration with no error at provision time. This test is the
tripwire that fails loudly BEFORE that ships.

Approach: **(A)** from the brief. ``core.containers.config.build_openclaw_config_dict``
is a pure, no-I/O builder (its docstring: "Pure (no I/O) so it can be
unit-tested without filesystem mocks") and is exactly what ``write_user_configs``
feeds to EFS. We build the real config dict in-process and assert directly on
the emitted shape — no source-text grepping needed.
"""

import pytest

from core.containers.config import build_openclaw_config_dict

_TOKEN = "tok-exec-host-guard"

# AIP ARNs only needed for the bedrock_claude path (build_provider_block raises
# without opus/sonnet/haiku keys). Dummy values — this test never invokes them.
_AIP_ARNS = {
    "opus": "arn:aws:bedrock:us-east-1:000000000000:inference-profile/us.opus",
    "sonnet": "arn:aws:bedrock:us-east-1:000000000000:inference-profile/us.sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:000000000000:inference-profile/us.haiku",
}


def _build(provider_choice: str, byo_provider: str | None = None) -> dict:
    return build_openclaw_config_dict(
        user_id="user_exec_host_guard",
        gateway_token=_TOKEN,
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        aip_arns=_AIP_ARNS if provider_choice == "bedrock_claude" else None,
    )


@pytest.mark.parametrize(
    "provider_choice,byo_provider",
    [
        ("chatgpt_oauth", None),
        ("bedrock_claude", None),
        ("byo_key", "anthropic"),
        ("byo_key", "openai"),
    ],
)
def test_emitted_config_keeps_exec_on_gateway_host(provider_choice, byo_provider):
    """exec must resolve to the gateway host across every provider path.

    Guards the two implicit properties that route exec to the gateway (where
    OpenClaw injects CLI-integration env vars like VERCEL_TOKEN):
      - no ``agents.defaults.sandbox`` (a sandbox host drops injected env), and
      - no pinned ``tools.exec.host`` other than gateway/auto.
    """
    cfg = _build(provider_choice, byo_provider)

    agents_defaults = (cfg.get("agents") or {}).get("defaults") or {}
    assert "sandbox" not in agents_defaults, (
        "agents.defaults.sandbox is set — exec would resolve to the sandbox host "
        "and drop skill/integration-injected env vars (e.g. VERCEL_TOKEN). Keep it "
        "unset, or fix CLI-token delivery to work under a sandbox host first."
    )

    exec_cfg = (cfg.get("tools") or {}).get("exec") or {}
    assert exec_cfg.get("host") in (None, "gateway", "auto"), (
        f"tools.exec.host={exec_cfg.get('host')!r} — pinning the exec host to "
        "anything but gateway/auto routes exec off the gateway, where OpenClaw "
        "injects CLI-integration env vars (VERCEL_TOKEN). It must stay unset "
        "(auto, which resolves to gateway because no sandbox is configured)."
    )
