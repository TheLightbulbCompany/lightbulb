import subprocess
import sys
from pathlib import Path

import pytest

from core.runtime import get_runtime, get_runtime_for_owner
from core.runtime.base import Capabilities, Runtime


def test_get_runtime_openclaw():
    rt = get_runtime("openclaw")
    assert rt.kind == "openclaw"
    assert isinstance(rt, Runtime)
    assert isinstance(rt.capabilities, Capabilities)
    assert rt.capabilities.desktop_pairing is True
    assert rt.capabilities.per_run_model is True


def test_get_runtime_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown runtime kind"):
        get_runtime("hermes")  # not registered until the Hermes adapter lands


def test_get_runtime_for_owner_defaults_openclaw():
    rt = get_runtime_for_owner("org_anything")
    assert rt.kind == "openclaw"


def test_get_runtime_is_singleton_per_kind():
    assert get_runtime("openclaw") is get_runtime("openclaw")


def test_sessions_is_the_gateway_pool(monkeypatch):
    sentinel = object()
    import core.runtime.openclaw.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "_get_pool", lambda: sentinel)
    assert get_runtime("openclaw").sessions is sentinel


def test_runtime_import_does_not_pull_core_containers():
    # core.containers builds AWS clients at import time; the runtime package
    # must stay import-light so adapters load without AWS plumbing. Runs in a
    # subprocess because the test session itself imports core.containers
    # elsewhere, which would mask a leak in-process.
    code = (
        "import sys\n"
        "import core.runtime\n"
        "import core.runtime.openclaw.adapter\n"
        "leaked = [m for m in sys.modules if m.startswith('core.containers')]\n"
        "assert not leaked, f'core.containers leaked into runtime import: {leaked}'\n"
    )
    backend_dir = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
