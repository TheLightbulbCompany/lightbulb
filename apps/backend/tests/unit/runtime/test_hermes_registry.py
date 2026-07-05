import subprocess
import sys
from pathlib import Path

import pytest

from core.runtime import get_runtime
from core.runtime.base import Capabilities, Runtime

# The H1.1-confirmed Hermes capability set. Kept as an explicit literal here so
# a drift in the adapter's flags fails this test loudly rather than silently
# tracking whatever the adapter happens to declare.
EXPECTED_HERMES_CAPABILITIES = Capabilities(
    desktop_pairing=False,
    device_exec_approval=False,
    resumable_stream=False,
    per_agent_workspace=True,
    multi_agent=True,
    durable_runs=False,
    structured_run_context=False,
    per_run_model=False,
)


def test_get_runtime_hermes_kind():
    assert get_runtime("hermes").kind == "hermes"


def test_hermes_capabilities_match_verified_set():
    caps = get_runtime("hermes").capabilities
    assert isinstance(caps, Capabilities)
    assert caps == EXPECTED_HERMES_CAPABILITIES
    # Spell each flag out so a failure names the offending capability.
    assert caps.desktop_pairing is False
    assert caps.device_exec_approval is False
    assert caps.resumable_stream is False
    assert caps.per_agent_workspace is True
    assert caps.multi_agent is True
    assert caps.durable_runs is False
    assert caps.structured_run_context is False
    assert caps.per_run_model is False


def test_hermes_satisfies_runtime_protocol():
    # runtime_checkable only checks member PRESENCE — the NotImplementedError
    # stubs satisfy the Protocol even though they raise when called.
    assert isinstance(get_runtime("hermes"), Runtime)


def test_hermes_is_singleton_per_kind():
    assert get_runtime("hermes") is get_runtime("hermes")


async def test_hermes_stub_method_raises_not_implemented():
    rt = get_runtime("hermes")
    with pytest.raises(NotImplementedError):
        await rt.call_rpc("org_x", "agents.list")


def test_hermes_sessions_stub_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        _ = get_runtime("hermes").sessions


def test_runtime_import_does_not_pull_core_containers_with_hermes():
    # get_runtime("hermes") now instantiates HermesRuntime, so importing
    # core.runtime pulls the hermes adapter module. Assert that neither the
    # registry nor the hermes adapter leaks core.containers (AWS clients at
    # import). Subprocess so a leak isn't masked by the parent test session.
    code = (
        "import sys\n"
        "import core.runtime\n"
        "import core.runtime.hermes.adapter\n"
        "core.runtime.get_runtime('hermes')\n"
        "leaked = [m for m in sys.modules if m.startswith('core.containers')]\n"
        "assert not leaked, f'core.containers leaked into hermes runtime import: {leaked}'\n"
    )
    backend_dir = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
