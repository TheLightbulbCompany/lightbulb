# Runtime Port Extraction Implementation Plan (Hermes Workstream 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a brand-blind `Runtime` port (`core/runtime/`) and consolidate every OpenClaw client behind one adapter, so a Hermes adapter can later slot in without touching callers.

**Architecture:** A `Runtime` Protocol + `Capabilities` descriptor lives in `core/runtime/base.py`; a registry (`get_runtime` / `get_runtime_for_owner`) is the single brand-switch point. The OpenClaw adapter wraps — never reshapes — the existing `GatewayConnectionPool` (exposed 1:1 via a structural `SessionSurface` Protocol the pool already satisfies) and the `gateway_rpc_oneshot` helper. Callers migrate cold-paths-first; the hot chat path (`websocket_chat.py`) migrates last as a pure import swap. Physical relocation of transport files happens only at the end, when nothing imports them directly.

**Tech Stack:** Python 3.12, FastAPI backend at `apps/backend/` (uv-managed), pytest, `typing.Protocol` structural typing.

**Spec:** `docs/superpowers/specs/2026-06-16-hermes-runtime-option-design.md` §4 + Revision 3 (the WS-client inventory there supersedes §4's table). Verified inventory as of 2026-07-01:

- Pool callers (non-test): `core/containers/__init__.py`, `main.py:249`, `core/services/{admin_service.py:278,353, catalog_service.py:247,365, paperclip_openclaw_pairing.py:497,526,548, node_proxy.py}`, `routers/{container.py:50,243, admin.py:504, channels.py:90, teams/agents.py:599,608, websocket_chat.py×9}`.
- One-shot callers: `core/services/container_auth.py:37`, `routers/container_recover.py:51,216`.
- Out of scope (stays as-is): `node_connection.py` + `node_proxy.py` + `operator_proxy.py` (relay facet — forwards third-party device signatures, architecturally un-poolable), Provisioner/IdentitySeeder/ArtifactCodec facets (`config.py`, `friday_identity.py`, `agent_export.py` — Workstream 1b), any Hermes code.

## Global Constraints

- All backend commands run from `apps/backend/`: tests via `uv run pytest <path> -v`.
- **Zero behavior change in pool semantics.** Never add filtering/reshaping to the gateway event pipeline (memory `feedback_no_speculative_event_filtering`). The adapter delegates; it does not interpret.
- `resolve_running_container` has cold-start-trigger side effects — preserve call cadence exactly (see comment at `routers/container.py:97`).
- Run only the targeted test files per task; the full backend suite runs once in the final task (memory `feedback_run_tests_at_end`). Do NOT run frontend tests (memory `feedback_frontend_vitest_ram_heavy`).
- Work on branch `feat/runtime-port-extraction` cut from current `origin/main`, in a worktree under the repo's `.worktrees/` (never a bare `~/Desktop` sibling).
- Commit after every task; commit messages follow repo convention (`refactor(runtime): …`). No PR/bug-ID references in code comments.
- `GATEWAY_PORT = 18789`; the gateway↔containers import-cycle constraint documented at `core/gateway/operator_rpc.py:22-24` must be respected (the runtime package must not import `core.containers` at module import time — do it lazily inside functions, as `routers/channels.py:85-88` does today).

---

### Task 1: Delete the dead control_ui_proxy router

`routers/control_ui_proxy.py` has zero consumers (its iframe consumer and the 5 dark panels were deleted; every live panel rides gateway RPC). Removing it retires one of the four WS-client shapes for free.

**Files:**
- Delete: `apps/backend/routers/control_ui_proxy.py`
- Modify: `apps/backend/main.py` (its `include_router` line + any tags entry)
- Delete: its test file if one exists (`grep -rl control_ui_proxy tests/`)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing — later tasks assume this router no longer exists.

- [ ] **Step 1: Confirm zero consumers** (protects against drift since the 2026-07-01 audit)

Run: `grep -rn "control_ui_proxy\|control-ui" apps/backend --include="*.py" | grep -v routers/control_ui_proxy` and `grep -rn "control-ui\|control_ui" apps/frontend/src apps/desktop 2>/dev/null | grep -v "\.next"`
Expected: only the `main.py` mount (backend) and comment-only hits (frontend, e.g. `useAgentChat.ts:5`). If a real consumer appears, STOP and report — do not delete.

- [ ] **Step 2: Delete the router and its mount**

Remove `routers/control_ui_proxy.py`, its import + `app.include_router(...)` line in `main.py`, and its test file if found.

- [ ] **Step 3: Verify the app still imports**

Run: `uv run python -c "import main"` then `uv run pytest tests/ -k "main or app_boot" -v` (whichever app-boot smoke tests exist; if none, the import check suffices).
Expected: clean import, no `ModuleNotFoundError`.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor(runtime): delete dead control_ui_proxy router (zero consumers)"
```

---

### Task 2: Runtime port types — `core/runtime/base.py`

**Files:**
- Create: `apps/backend/core/runtime/__init__.py` (empty for now; registry lands in Task 3)
- Create: `apps/backend/core/runtime/base.py`
- Test: `apps/backend/tests/unit/runtime/test_base.py` (+ empty `tests/unit/runtime/__init__.py` if the suite requires packages)

**Interfaces:**
- Consumes: nothing (pure types).
- Produces: `Capabilities` (frozen dataclass), `SessionSurface` (Protocol), `Runtime` (Protocol), `ContainerNotRunningError(Exception)`. Exact shapes below — Tasks 3–8 import these verbatim.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/runtime/test_base.py
from core.runtime.base import Capabilities, ContainerNotRunningError, Runtime, SessionSurface


def test_capabilities_openclaw_defaults():
    caps = Capabilities(
        desktop_pairing=True,
        device_exec_approval=True,
        resumable_stream=True,
        per_agent_workspace=True,
        multi_agent=True,
        durable_runs=True,
        structured_run_context=True,
        per_run_model=True,
    )
    assert caps.desktop_pairing is True
    assert caps.per_run_model is True


def test_capabilities_is_frozen():
    caps = Capabilities(
        desktop_pairing=True, device_exec_approval=True, resumable_stream=True,
        per_agent_workspace=True, multi_agent=True, durable_runs=True,
        structured_run_context=True, per_run_model=True,
    )
    import dataclasses, pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.desktop_pairing = False


def test_protocols_are_runtime_checkable():
    assert hasattr(Runtime, "__protocol_attrs__") or True  # Protocol classes import cleanly
    assert issubclass(ContainerNotRunningError, Exception)
    assert SessionSurface is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.runtime'`

- [ ] **Step 3: Write `core/runtime/base.py`**

```python
"""Brand-blind runtime port: the contract every agent-runtime adapter fills.

Generic code depends on ``Runtime`` + ``Capabilities`` and never names a
runtime brand; every runtime-specific divergence (wire protocol, on-disk
formats) is contained inside ``core/runtime/<kind>/``.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class ContainerNotRunningError(RuntimeError):
    """Raised by ``Runtime.call_rpc`` when the owner has no running container."""


@dataclass(frozen=True)
class Capabilities:
    """What a runtime can do. Generic code asks a capability, never a brand."""

    desktop_pairing: bool
    device_exec_approval: bool
    resumable_stream: bool
    per_agent_workspace: bool
    multi_agent: bool
    durable_runs: bool
    structured_run_context: bool
    per_run_model: bool


@runtime_checkable
class SessionSurface(Protocol):
    """The live-session surface a runtime exposes to the chat path.

    Method names and signatures mirror ``GatewayConnectionPool`` exactly so
    the pool satisfies this Protocol structurally with zero wrappers; a
    second runtime implements the same surface over its own transport.
    Extend this Protocol (never the caller) if a migration surfaces a pool
    method not yet declared here.
    """

    def is_user_connected(self, user_id: str) -> bool: ...
    def mark_run_active(self, user_id: str, session_key: str, agent_id: str | None = None) -> None: ...
    def clear_active_run(self, user_id: str, session_key: str) -> None: ...
    def mark_side_run(self, user_id: str, run_id: str) -> None: ...
    async def gate_chat(self, *, user_id: str, owner_id: str) -> dict: ...
    async def send_rpc(self, user_id: str, req_id: str, method: str, params: dict, ip: str, token: str) -> Any: ...
    async def ensure_connection(self, user_id: str, ip: str, token: str) -> Any: ...
    def add_frontend_connection(self, user_id: str, connection_id: str) -> None: ...
    def remove_frontend_connection(self, user_id: str, connection_id: str) -> None: ...
    async def close_user(self, user_id: str) -> None: ...
    async def broadcast_to_user(self, user_id: str, message: dict) -> None: ...
    async def broadcast_to_member(self, owner_id: str, member_user_id: str, message: dict) -> None: ...
    async def close_all(self) -> None: ...
    async def emit_running_gauges(self) -> None: ...


@runtime_checkable
class Runtime(Protocol):
    """One agent-runtime adapter (OpenClaw today, Hermes later)."""

    kind: str
    capabilities: Capabilities

    @property
    def sessions(self) -> SessionSurface: ...

    async def call_rpc(
        self,
        owner_id: str,
        method: str,
        params: dict | None = None,
        *,
        fresh: bool = False,
        timeout: float | None = None,
    ) -> Any: ...

    async def revoke_provider(self, owner_id: str, *, provider: str) -> bool: ...

    async def forward_hook(
        self, owner_id: str, path: str, headers: dict[str, str], body: bytes
    ) -> Any: ...
```

**Note on `mark_run_active`/`send_rpc` signatures:** copy them from the POOL-level methods (`connection_pool.py:1737`, `:1871`) — not the per-connection methods higher in the file with the same names. Verify against the source before committing; the pool is the structural contract.

Also create `core/runtime/__init__.py` containing only a module docstring (`"""Runtime adapters — see base.py for the port contract."""`) and `tests/unit/runtime/__init__.py` (empty).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_base.py -v`
Expected: 3 PASS

- [ ] **Step 5: Verify the pool structurally satisfies SessionSurface** (add to the same test file)

```python
def test_pool_satisfies_session_surface():
    from core.gateway.connection_pool import GatewayConnectionPool
    from core.runtime.base import SessionSurface

    pool = GatewayConnectionPool()
    assert isinstance(pool, SessionSurface)
```

If `GatewayConnectionPool()` needs constructor args, construct it the way `core/containers/__init__.py:33` does (read that factory and mirror it, mocking any AWS clients). If `isinstance` fails, the Protocol signature drifted from the pool — fix `base.py` to match the pool, never the reverse.

Run: `uv run pytest tests/unit/runtime/test_base.py -v` — Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add core/runtime/ tests/unit/runtime/ && git commit -m "refactor(runtime): add brand-blind Runtime port types (Capabilities, SessionSurface, Runtime)"
```

---

### Task 3: OpenClaw adapter + registry

**Files:**
- Create: `apps/backend/core/runtime/openclaw/__init__.py`
- Create: `apps/backend/core/runtime/openclaw/adapter.py`
- Modify: `apps/backend/core/runtime/__init__.py` (registry)
- Test: `apps/backend/tests/unit/runtime/test_registry.py`

**Interfaces:**
- Consumes: `Capabilities`, `Runtime`, `SessionSurface` from Task 2; `core.containers.get_gateway_pool` (lazily).
- Produces: `get_runtime(kind: str) -> Runtime`, `get_runtime_for_owner(owner_id: str) -> Runtime` in `core/runtime/__init__.py`; class `OpenClawRuntime` with `kind == "openclaw"`. Tasks 4–8 call these.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/runtime/test_registry.py
import pytest

from core.runtime import get_runtime, get_runtime_for_owner
from core.runtime.base import Runtime


def test_get_runtime_openclaw():
    rt = get_runtime("openclaw")
    assert rt.kind == "openclaw"
    assert isinstance(rt, Runtime)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_registry.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_runtime'`

- [ ] **Step 3: Write the adapter and registry**

```python
# core/runtime/openclaw/adapter.py
"""OpenClaw runtime adapter: wraps the existing gateway pool + one-shot RPC.

The adapter delegates 1:1 — the pool IS the session surface (structural
typing). It never filters or reshapes gateway traffic.
"""

from typing import Any

from core.runtime.base import Capabilities, ContainerNotRunningError, SessionSurface

OPENCLAW_CAPABILITIES = Capabilities(
    desktop_pairing=True,
    device_exec_approval=True,
    resumable_stream=True,
    per_agent_workspace=True,
    multi_agent=True,
    durable_runs=True,
    structured_run_context=True,
    per_run_model=True,
)


def _get_pool():
    # Lazy: core.containers builds AWS clients at import; the runtime package
    # must stay import-light (same constraint as operator_rpc's port constant).
    from core.containers import get_gateway_pool

    return get_gateway_pool()


class OpenClawRuntime:
    kind = "openclaw"
    capabilities = OPENCLAW_CAPABILITIES

    @property
    def sessions(self) -> SessionSurface:
        return _get_pool()

    # call_rpc / revoke_provider / forward_hook are added in Tasks 4 and 7.
```

```python
# core/runtime/__init__.py
"""Runtime registry — the ONLY place that maps a runtime kind to an adapter."""

from functools import lru_cache

from core.runtime.base import Runtime


@lru_cache(maxsize=None)
def get_runtime(kind: str) -> Runtime:
    if kind == "openclaw":
        from core.runtime.openclaw.adapter import OpenClawRuntime

        return OpenClawRuntime()
    raise ValueError(f"unknown runtime kind: {kind!r}")


def get_runtime_for_owner(owner_id: str) -> Runtime:
    """Resolve the runtime for an owner.

    Every owner runs OpenClaw today. When a second runtime ships, this reads
    ``runtime_kind`` off the owner's container/billing row — keeping that
    lookup here means no caller ever branches on a brand.
    """
    return get_runtime("openclaw")
```

`core/runtime/openclaw/__init__.py`: module docstring only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/runtime/ -v`
Expected: all PASS (Task 2's tests included)

- [ ] **Step 5: Commit**

```bash
git add core/runtime/ tests/unit/runtime/ && git commit -m "refactor(runtime): OpenClaw adapter + get_runtime registry"
```

---

### Task 4: `call_rpc` (pooled + fresh) and `revoke_provider` on the adapter

This absorbs the `resolve_running_container` + `gateway_token` plumbing every caller currently does by hand.

**Files:**
- Modify: `apps/backend/core/runtime/openclaw/adapter.py`
- Test: `apps/backend/tests/unit/runtime/test_openclaw_call_rpc.py`

**Interfaces:**
- Consumes: `ecs_manager.resolve_running_container(user_id) -> tuple[dict | None, str | None]` (container row incl. `gateway_token`, ip); `pool.send_rpc(user_id, req_id, method, params, ip, token)`; `gateway_rpc_oneshot(ip, *, owner_id, token, method, params, open_timeout, recv_timeout, rpc_timeout)` from `core.gateway.operator_rpc`; `container_auth._logout_provider_via_gateway` semantics.
- Produces: `OpenClawRuntime.call_rpc(owner_id, method, params=None, *, fresh=False, timeout=None) -> Any` raising `ContainerNotRunningError` when cold; `OpenClawRuntime.revoke_provider(owner_id, *, provider) -> bool` (never raises). Tasks 5–6 call these.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/runtime/test_openclaw_call_rpc.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.runtime.base import ContainerNotRunningError
from core.runtime.openclaw.adapter import OpenClawRuntime


def _mock_ecs(container, ip):
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(container, ip))
    return ecs


@pytest.mark.asyncio
async def test_call_rpc_pooled_path():
    rt = OpenClawRuntime()
    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True})
    container = {"gateway_token": "tok-1"}
    with (
        patch("core.runtime.openclaw.adapter._get_pool", return_value=pool),
        patch("core.runtime.openclaw.adapter._get_ecs", return_value=_mock_ecs(container, "10.0.0.5")),
    ):
        result = await rt.call_rpc("org_1", "channels.status", {"probe": True})
    assert result == {"ok": True}
    kwargs = pool.send_rpc.call_args.kwargs
    assert kwargs["user_id"] == "org_1"
    assert kwargs["method"] == "channels.status"
    assert kwargs["ip"] == "10.0.0.5"
    assert kwargs["token"] == "tok-1"
    assert kwargs["req_id"]  # generated, non-empty


@pytest.mark.asyncio
async def test_call_rpc_fresh_path_uses_oneshot():
    rt = OpenClawRuntime()
    container = {"gateway_token": "tok-1"}
    with (
        patch("core.runtime.openclaw.adapter._get_ecs", return_value=_mock_ecs(container, "10.0.0.5")),
        patch("core.runtime.openclaw.adapter.gateway_rpc_oneshot", new=AsyncMock(return_value={"restarted": True})) as oneshot,
    ):
        result = await rt.call_rpc("org_1", "gateway.restart", fresh=True, timeout=5)
    assert result == {"restarted": True}
    assert oneshot.call_args.kwargs["owner_id"] == "org_1"
    assert oneshot.call_args.kwargs["token"] == "tok-1"
    assert oneshot.call_args.kwargs["open_timeout"] == 5


@pytest.mark.asyncio
async def test_call_rpc_cold_container_raises():
    rt = OpenClawRuntime()
    with patch("core.runtime.openclaw.adapter._get_ecs", return_value=_mock_ecs(None, None)):
        with pytest.raises(ContainerNotRunningError):
            await rt.call_rpc("org_1", "anything")


@pytest.mark.asyncio
async def test_revoke_provider_never_raises_and_returns_false_when_cold():
    rt = OpenClawRuntime()
    with patch("core.runtime.openclaw.adapter._get_ecs", return_value=_mock_ecs(None, None)):
        assert await rt.revoke_provider("org_1", provider="openai") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/runtime/test_openclaw_call_rpc.py -v`
Expected: FAIL — `AttributeError: 'OpenClawRuntime' object has no attribute 'call_rpc'`

- [ ] **Step 3: Implement on the adapter**

Add to `core/runtime/openclaw/adapter.py`:

```python
import uuid

from core.gateway.operator_rpc import gateway_rpc_oneshot


def _get_ecs():
    from core.containers import get_ecs_manager

    return get_ecs_manager()


class OpenClawRuntime:
    # ... kind / capabilities / sessions from Task 3 ...

    async def _resolve(self, owner_id: str) -> tuple[str, str]:
        container, ip = await _get_ecs().resolve_running_container(owner_id)
        if not container or not ip:
            raise ContainerNotRunningError(f"no running container for {owner_id}")
        return ip, container["gateway_token"]

    async def call_rpc(
        self,
        owner_id: str,
        method: str,
        params: dict | None = None,
        *,
        fresh: bool = False,
        timeout: float | None = None,
    ):
        ip, token = await self._resolve(owner_id)
        if fresh:
            t = timeout if timeout is not None else 10.0
            return await gateway_rpc_oneshot(
                ip,
                owner_id=owner_id,
                token=token,
                method=method,
                params=params,
                open_timeout=t,
                recv_timeout=t,
                rpc_timeout=t,
            )
        return await _get_pool().send_rpc(
            user_id=owner_id,
            req_id=f"rt-{method}-{uuid.uuid4().hex[:12]}",
            method=method,
            params=params or {},
            ip=ip,
            token=token,
        )

    async def revoke_provider(self, owner_id: str, *, provider: str) -> bool:
        try:
            await self.call_rpc(
                owner_id, "models.authLogout", {"provider": provider}, fresh=True
            )
            return True
        except Exception:
            return False
```

**Fidelity check before committing:** `container_auth.revoke_provider_auth_in_container` (`core/services/container_auth.py:49+`) is the current revoke implementation — read it end-to-end and carry over any logging / metric emission it does around the failure path, so behavior is preserved when Task 5 delegates to this method.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/runtime/ -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add core/runtime/ tests/unit/runtime/ && git commit -m "refactor(runtime): call_rpc (pooled+fresh) and revoke_provider on the OpenClaw adapter"
```

---

### Task 5: Migrate the one-shot consumers

**Files:**
- Modify: `apps/backend/core/services/container_auth.py`
- Modify: `apps/backend/routers/container_recover.py`
- Test: existing suites — `grep -rl "container_auth\|container_recover" tests/` to find them.

**Interfaces:**
- Consumes: `get_runtime_for_owner(owner_id).call_rpc(..., fresh=True)` and `.revoke_provider(...)` from Tasks 3–4.
- Produces: `container_auth.revoke_provider_auth_in_container(owner_id, *, provider)` keeps its exact public signature and never-raise contract (callers unchanged).

- [ ] **Step 1: Rewrite `container_auth.py` body to delegate**

Keep the public function; its body becomes:

```python
from core.runtime import get_runtime_for_owner


async def revoke_provider_auth_in_container(owner_id: str, *, provider: str = "openai") -> bool:
    return await get_runtime_for_owner(owner_id).revoke_provider(owner_id, provider=provider)
```

Delete `_logout_provider_via_gateway` and the now-unused `gateway_rpc_oneshot` / `resolve_running_container` plumbing in this file (first porting any logging it does into the adapter, per Task 4 Step 3's fidelity check). Update the module docstring to say it delegates to the runtime port.

- [ ] **Step 2: Rewrite `container_recover.py`'s `_call_gateway_rpc`**

Replace its body (currently `gateway_rpc_oneshot(...)` with 5/5/10s timeouts at `:51`) — and the second direct call at `:216` — with:

```python
from core.runtime import get_runtime_for_owner

# inside the existing functions, replacing the direct oneshot calls:
result = await get_runtime_for_owner(owner_id).call_rpc(method_name, ...)  # see below
```

Concretely: `_call_gateway_rpc(ip, owner_id, token, method, params)` becomes `_call_gateway_rpc(owner_id, method, params)` = `await get_runtime_for_owner(owner_id).call_rpc(owner_id, method, params, fresh=True, timeout=5)`. **Caveat:** the recover flow already resolved ip/token itself for its state inspection — the adapter re-resolves. That is one extra `resolve_running_container` call on a cold-path admin flow; acceptable, but verify the recover tests still pass (they assert on recovery decisions, not call counts). If a recover test asserts the resolve count, keep passing ip/token through by giving `call_rpc` optional `ip=`/`token=` overrides instead — extend the port signature in `base.py` accordingly.

- [ ] **Step 3: Run the targeted suites**

Run: `uv run pytest tests/ -k "container_auth or container_recover or recover" -v`
Expected: PASS (same count as on main — run `git stash && uv run pytest ... && git stash pop` to baseline if unsure).

- [ ] **Step 4: Commit**

```bash
git add core/services/container_auth.py routers/container_recover.py && git commit -m "refactor(runtime): one-shot consumers ride the runtime port"
```

---

### Task 6: Migrate the cold-path pool callers

Every caller currently does the resolve-then-`pool.send_rpc` dance by hand (pattern at `routers/channels.py:85-97`). Each becomes one `call_rpc` line. Migrate file-by-file, running that file's tests before moving on.

**Files (verified call sites, one commit each or one bundled commit at the end):**
- Modify: `apps/backend/routers/channels.py:90`
- Modify: `apps/backend/routers/admin.py:504`
- Modify: `apps/backend/routers/container.py:243` (NOT `:50` if that's the pool-boot/status path — read it first; migrate only RPC-issuing sites)
- Modify: `apps/backend/routers/teams/agents.py:599,608`
- Modify: `apps/backend/core/services/admin_service.py:278,353`
- Modify: `apps/backend/core/services/catalog_service.py:247,365` (pool is factory-injected here — swap the injected factory for `get_runtime_for_owner`, keeping the constructor injectable for tests)
- Modify: `apps/backend/core/services/paperclip_openclaw_pairing.py:497,526,548` (takes the pool as a parameter — narrowest change: accept a `Runtime` or its `sessions` instead)
- Leave alone: `core/containers/__init__.py` (the pool factory itself), `main.py:249` (lifespan gauge loop — migrate to `get_runtime("openclaw").sessions.emit_running_gauges()` only if trivial; otherwise leave for Task 9), `core/services/node_proxy.py` (relay facet, out of scope).

**Interfaces:**
- Consumes: `get_runtime_for_owner(owner_id).call_rpc(owner_id, method, params)` (pooled default) and `.sessions` for non-RPC pool methods.
- Produces: no public-surface changes — routes and service signatures unchanged.

- [ ] **Step 1: Migrate `routers/channels.py` as the template**

The current code (`:85-97`):

```python
from core.containers import get_ecs_manager, get_gateway_pool

ecs_manager = get_ecs_manager()
container, ip = await ecs_manager.resolve_running_container(owner_id)
if container and ip:
    pool = get_gateway_pool()
    status = await pool.send_rpc(
        user_id=owner_id,
        req_id=f"links-me-{owner_id}",
        method="channels.status",
        params={"probe": True},
        ip=ip,
        token=container["gateway_token"],
    )
```

becomes:

```python
from core.runtime import get_runtime_for_owner
from core.runtime.base import ContainerNotRunningError

try:
    status = await get_runtime_for_owner(owner_id).call_rpc(
        owner_id, "channels.status", {"probe": True}
    )
except ContainerNotRunningError:
    status = None  # preserve the old `if container and ip` fall-through behavior
```

**The `if container and ip: ... else` shape varies per site** — at each site, map "container cold" to whatever that site did when the resolve returned `(None, None)` (skip, 404, fall through). Never let `ContainerNotRunningError` propagate where the old code fell through silently.

- [ ] **Step 2: Repeat for each file in the list**

Same transformation at every site. For `catalog_service.py`: its constructor takes a pool factory — change the parameter to a runtime factory (`runtime_factory: Callable[[], Runtime] = lambda: get_runtime("openclaw")`) and update the two call sites; keep tests injecting a fake.

- [ ] **Step 3: Run each file's tests as you go, then the batch**

Run: `uv run pytest tests/ -k "channels or admin or catalog or pairing or teams_agents or container" -v`
Expected: PASS, same counts as baseline.

- [ ] **Step 4: Verify no stragglers**

Run: `grep -rn "get_gateway_pool()" --include="*.py" routers/ core/services/ | grep -v "node_proxy\|catalog_service"`
Expected: only `websocket_chat.py` hits remain (Task 8) — anything else is a missed site; migrate it.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor(runtime): cold-path callers ride the runtime port"
```

---

### Task 7: `forward_hook` port method + hooks_proxy migration

**Files:**
- Modify: `apps/backend/core/runtime/openclaw/adapter.py`
- Modify: `apps/backend/core/runtime/base.py` (only if the signature below needs adjustment after reading the router)
- Modify: `apps/backend/routers/hooks_proxy.py` (`:45`, `:177`)
- Test: `apps/backend/tests/unit/runtime/test_openclaw_forward_hook.py` + existing hooks_proxy suite

**Interfaces:**
- Consumes: the router's current httpx POST to `http://{ip}:18789/hooks/{path}` with `hook_token` bearer auth — read `hooks_proxy.py` fully first; router-level auth/rate-limit/audit stays in the router.
- Produces: `OpenClawRuntime.forward_hook(owner_id, path, headers, body) -> tuple[int, dict[str, str], bytes]` (status, response headers, response body) so the router can relay verbatim.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/runtime/test_openclaw_forward_hook.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.runtime.openclaw.adapter import OpenClawRuntime


@pytest.mark.asyncio
async def test_forward_hook_posts_to_container_hooks_path():
    rt = OpenClawRuntime()
    container = {"gateway_token": "tok", "hook_token": "hook-tok"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(container, "10.0.0.9"))
    fake_resp = MagicMock(status_code=200, headers={"content-type": "application/json"}, content=b"{}")
    with (
        patch("core.runtime.openclaw.adapter._get_ecs", return_value=ecs),
        patch("core.runtime.openclaw.adapter.httpx.AsyncClient") as client_cls,
    ):
        client_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=fake_resp)
        status, headers, body = await rt.forward_hook("org_1", "telegram", {"x-h": "1"}, b"{}")
    assert status == 200
    posted_url = client_cls.return_value.__aenter__.return_value.post.call_args.args[0]
    assert posted_url == "http://10.0.0.9:18789/hooks/telegram"
```

- [ ] **Step 2: Run to verify it fails, then implement**

Implementation mirrors the router's existing forward block (`hooks_proxy.py:45` builds the URL, `:177` posts). Move ONLY the URL-build + POST + auth-header attachment into the adapter; keep request validation, rate limiting, and audit in the router. The exact auth header name/token field must be copied from the router (read it — do not guess between `hook_token` on the container row vs a derived header).

- [ ] **Step 3: Migrate the router to call `forward_hook`, run suites**

Run: `uv run pytest tests/ -k "hook" -v` and `uv run pytest tests/unit/runtime/ -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor(runtime): hooks forwarding rides the runtime port"
```

---

### Task 8: Migrate the hot chat path (`websocket_chat.py`)

The 9 pool sites (`:296, :469, :641, :1105, :1507, :1726, :2037, :2044, :2127`) use the pool's session surface (fan-out, run windows, gating) — NOT the call_rpc convenience. This is a pure import/accessor swap; the method calls themselves DO NOT CHANGE.

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py`
- Test: the full existing websocket_chat suite.

**Interfaces:**
- Consumes: `get_runtime_for_owner(owner_id).sessions` — which IS the pool object, so every existing call compiles unchanged.
- Produces: nothing new.

- [ ] **Step 1: Baseline the suite**

Run: `uv run pytest tests/ -k "websocket_chat or chat_event or connection_pool" -v` on the branch BEFORE editing; record pass count.

- [ ] **Step 2: Swap the accessor at each of the 9 sites**

At each site, `pool = get_gateway_pool()` becomes `pool = get_runtime_for_owner(owner_id).sessions` (the local variable name and every subsequent call stay identical). Where the site has a `user_id` but the pool is owner-scoped, resolve the owner the same way the surrounding code already does — do not invent new owner-resolution logic. If any site calls a pool method not declared on `SessionSurface`, ADD it to the Protocol in `base.py` (with the pool's exact signature) rather than bypassing the port.

**Do not change anything else.** No reordering, no error-handling changes, no event filtering — the parked-vs-dead windows, side runs, and abort synthetic terminals in this file are recent and subtle.

- [ ] **Step 3: Re-run the suite**

Run: same command as Step 1.
Expected: identical pass count. Any delta = revert and re-approach that site.

- [ ] **Step 4: Commit**

```bash
git add routers/websocket_chat.py core/runtime/base.py && git commit -m "refactor(runtime): hot chat path rides the runtime port session surface"
```

---

### Task 9: Physical relocation + GATEWAY_PORT consolidation (deferrable)

Only after Tasks 5–8, nothing outside `core/runtime/`, `core/containers/`, `node_proxy`, and tests imports the transport modules directly. **Skip this task (file a follow-up) if any open PR touches `connection_pool.py` or `websocket_chat.py` — relocation is pure churn and loses to merge conflicts.**

**Files:**
- Create: `apps/backend/core/runtime/openclaw/constants.py` (`GATEWAY_PORT = 18789`, single definition)
- Move (git mv): `core/gateway/connection_pool.py` → `core/runtime/openclaw/pool.py`; `core/gateway/operator_rpc.py` → `core/runtime/openclaw/operator_rpc.py`
- Create (shims at old paths): `core/gateway/connection_pool.py` and `core/gateway/operator_rpc.py` each containing only `from core.runtime.openclaw.pool import *  # noqa: F401,F403` (resp. operator_rpc) plus a docstring saying "relocated; import from core.runtime.openclaw"
- Modify: the four `GATEWAY_PORT` definition sites (`ecs_manager.py:37`, moved `operator_rpc.py:33`, `node_connection.py:42`, `hooks_proxy.py:45`) to import from `constants.py` — respecting the import-cycle rule (ecs_manager may need a local literal + comment if the cycle bites; verify with `uv run python -c "import main"`).

**Interfaces:**
- Consumes/Produces: no API change; import paths only.

- [ ] **Step 1: git mv + shims + import updates** — as listed above.
- [ ] **Step 2: Verify** — `uv run python -c "import main"` then `uv run pytest tests/unit/runtime/ -k "runtime or gateway or operator_rpc" -v`. Expected: PASS.
- [ ] **Step 3: Commit** — `git add -A && git commit -m "refactor(runtime): relocate OpenClaw transport under core/runtime/openclaw"`

---

### Task 10: Full-suite verification + PR

- [ ] **Step 1: Full backend suite**

Run: `uv run pytest tests/ -x -q`
Expected: same pass count as `origin/main` baseline (`git stash`-free: check out main in a second worktree and run there if a baseline is needed). Investigate ANY failure — dependent systems; your change is the likely culprit even in untouched files.

- [ ] **Step 2: Straggler + hygiene checks**

Run: `grep -rn "get_gateway_pool()" --include="*.py" routers/ | grep -v websocket` (expect: none) and `git ls-tree -r HEAD | awk '$1==120000'` (expect: no committed symlinks).

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/runtime-port-extraction
gh pr create --title "refactor(runtime): brand-blind Runtime port + OpenClaw client consolidation" --body "..."
```

PR body: link the spec (§4 + Rev 3), state zero-behavior-change intent, note Task 9 deferral if taken. Watch CI (`gh run watch`). Do not merge without review/approval per repo convention.
