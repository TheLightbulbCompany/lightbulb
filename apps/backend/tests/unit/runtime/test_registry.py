import logging
import subprocess
import sys
from pathlib import Path

import pytest

import core.runtime as runtime_mod
from core.runtime import get_runtime, get_runtime_for_owner, prime_runtime_kind
from core.runtime.base import Capabilities, Runtime


@pytest.fixture(autouse=True)
def _clear_runtime_kind_cache():
    """runtime_kind is process-local + immutable; reset between tests so one
    test's primed owner never bleeds into another."""
    runtime_mod._RUNTIME_KIND_CACHE.clear()
    yield
    runtime_mod._RUNTIME_KIND_CACHE.clear()


def test_get_runtime_openclaw():
    rt = get_runtime("openclaw")
    assert rt.kind == "openclaw"
    assert isinstance(rt, Runtime)
    assert isinstance(rt.capabilities, Capabilities)
    assert rt.capabilities.desktop_pairing is True
    assert rt.capabilities.per_run_model is True


def test_get_runtime_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown runtime kind"):
        get_runtime("frobnicate")  # a kind with no registered adapter


def test_get_runtime_for_owner_defaults_openclaw():
    rt = get_runtime_for_owner("org_anything")
    assert rt.kind == "openclaw"


def test_get_runtime_for_owner_cache_miss_defaults_openclaw():
    # (a) An owner never primed → cache miss → the safe default. No DDB read,
    # fully sync, so Phase-B call sites stay sync.
    assert "org_never_primed" not in runtime_mod._RUNTIME_KIND_CACHE
    assert get_runtime_for_owner("org_never_primed").kind == "openclaw"


async def test_primed_hermes_row_resolves_hermes_runtime(monkeypatch):
    # (b) A primed row carrying runtime_kind="hermes" routes to the Hermes
    # runtime. The Hermes adapter is not registered yet, so stand in a fake
    # via the registry the resolver calls.
    fake_hermes = object()
    real_get_runtime = runtime_mod.get_runtime

    def _fake_get_runtime(kind):
        if kind == "hermes":
            return fake_hermes
        return real_get_runtime(kind)

    monkeypatch.setattr(runtime_mod, "get_runtime", _fake_get_runtime)

    kind = await prime_runtime_kind("org_h", container_row={"runtime_kind": "hermes"})
    assert kind == "hermes"
    assert get_runtime_for_owner("org_h") is fake_hermes


async def test_prime_reads_container_repo_when_row_absent(monkeypatch):
    # prime does an async container_repo read when no row is handed in.
    from core.repositories import container_repo

    async def _fake_get(owner_id):
        assert owner_id == "org_read"
        return {"owner_id": owner_id, "runtime_kind": "openclaw"}

    monkeypatch.setattr(container_repo, "get_by_owner_id", _fake_get)
    kind = await prime_runtime_kind("org_read")
    assert kind == "openclaw"
    assert runtime_mod._RUNTIME_KIND_CACHE["org_read"] == "openclaw"


async def test_prime_missing_runtime_kind_field_defaults_openclaw():
    # A legacy row without the field (H3.1 has not written it yet) → openclaw.
    kind = await prime_runtime_kind("org_legacy", container_row={"owner_id": "org_legacy"})
    assert kind == "openclaw"
    assert get_runtime_for_owner("org_legacy").kind == "openclaw"


async def test_prime_repo_error_falls_back_openclaw_and_never_raises(monkeypatch, caplog):
    # (c) A repo failure during prime → cache openclaw + logged; the sync
    # resolver still returns openclaw and never raises. This is what protects
    # container_auth's never-raise contract.
    from core.repositories import container_repo

    async def _boom(owner_id):
        raise RuntimeError("ddb exploded")

    monkeypatch.setattr(container_repo, "get_by_owner_id", _boom)

    with caplog.at_level(logging.WARNING):
        kind = await prime_runtime_kind("org_err")  # no row → async read → raises internally
    assert kind == "openclaw"
    assert get_runtime_for_owner("org_err").kind == "openclaw"
    assert "org_err" in caplog.text


async def test_primed_unknown_kind_falls_back_openclaw(caplog):
    # (d) An unknown kind lands in the cache (e.g. a future kind whose adapter
    # is not registered in this process) → get_runtime raises → the resolver
    # falls back to openclaw with a warning, never raising.
    await prime_runtime_kind("org_u", container_row={"runtime_kind": "frobnicate"})
    with caplog.at_level(logging.WARNING):
        rt = get_runtime_for_owner("org_u")
    assert rt.kind == "openclaw"
    assert "frobnicate" in caplog.text


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
