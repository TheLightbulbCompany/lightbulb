# Piece 3 — Clean Handoff / HA Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a backend restart/deploy a clean handoff instead of a 1–5 min outage — bounded graceful shutdown, drained in-flight work, a 503-during-drain gate, a cheap liveness health check, and rolling-deploy CDK config — all correct at `desiredCount=1`.

**Architecture:** Spec `docs/superpowers/specs/2026-05-30-multi-instance-backend-architecture-design.md` §3 (Piece 3). Timing ladder: app `asyncio.timeout(20)` < uvicorn `--timeout-graceful-shutdown 25` < ECS container `stopTimeout 30s`, with ALB `deregistrationDelay 25s` ≤ stopTimeout. Deploy uses `minHealthyPercent=100`/`maxHealthyPercent=200` so ECS starts+health-checks the new task before stopping the old.

**Tech Stack:** FastAPI/uvicorn (Python 3.12, `uv`), pytest; AWS CDK (TypeScript), `aws-cdk-lib/assertions`.

**Scope note:** RPC idempotency keys are deferred to the scaling plans (they only matter at N>1 — a retry can hit a different instance). This plan is the single shippable "clean handoff" unit.

**Names used across tasks (keep consistent):**
- `core/middleware/shutdown_aware.py`: module bool `_shutting_down`; `set_shutting_down(value: bool)`; `is_shutting_down() -> bool`; `class ShutdownAwareMiddleware(BaseHTTPMiddleware)`; `SHUTDOWN_WHITELIST = {"/health"}`.
- `core/services/update_service.py`: module `_worker_stop: asyncio.Event`; `request_worker_stop()`; `reset_worker_stop()`.
- `main.py`: module `_started: bool` + `app.state` not used for this.
- `connection_pool.py`: `_RPC_DRAIN_TIMEOUT = 5.0`.

> Decision (corrects a grounding suggestion): the shutdown flag is a **module-level bool**, not a `ContextVar` — it is process-global state, not request-scoped. And bounded waits use `asyncio.wait_for(...)` / `asyncio.wait(..., timeout=...)`, never the invalid `asyncio.timeout(n)(coro)` form.

---

### Task 1: Shutdown-aware flag + middleware

**Files:**
- Create: `apps/backend/core/middleware/shutdown_aware.py`
- Test: `apps/backend/tests/unit/core/test_shutdown_aware_middleware.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/core/test_shutdown_aware_middleware.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import shutdown_aware
from core.middleware.shutdown_aware import ShutdownAwareMiddleware, set_shutting_down


@pytest.fixture(autouse=True)
def _reset_flag():
    set_shutting_down(False)
    yield
    set_shutting_down(False)


def _app() -> TestClient:
    app = FastAPI()
    app.add_middleware(ShutdownAwareMiddleware)

    @app.get("/health")
    def health():
        return {"status": "healthy"}

    @app.post("/ws/message")
    def ws_message():
        return {"ok": True}

    return TestClient(app)


def test_passes_all_requests_when_not_shutting_down():
    c = _app()
    assert c.get("/health").status_code == 200
    assert c.post("/ws/message").status_code == 200


def test_503s_new_business_requests_during_shutdown():
    c = _app()
    set_shutting_down(True)
    r = c.post("/ws/message")
    assert r.status_code == 503
    assert r.json()["detail"] == "Service shutting down"


def test_health_check_still_passes_during_shutdown():
    c = _app()
    set_shutting_down(True)
    assert c.get("/health").status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_shutdown_aware_middleware.py -v`
Expected: FAIL — `ModuleNotFoundError: core.middleware.shutdown_aware`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/backend/core/middleware/shutdown_aware.py
"""Process-wide graceful-shutdown gate.

During lifespan shutdown we set a process-global flag; this middleware then
returns 503 to NEW business requests (so the ALB/clients stop sending work)
while letting in-flight requests finish. The health check is whitelisted so
the task stays "healthy" and is drained via ECS deregistration, not flapped
out as unhealthy. The flag is a module-level bool (process-global state), not
a ContextVar (which is request-scoped).
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_shutting_down = False

# Paths that MUST keep working during drain (ALB health check).
SHUTDOWN_WHITELIST = {"/health"}


def set_shutting_down(value: bool) -> None:
    global _shutting_down
    _shutting_down = value


def is_shutting_down() -> bool:
    return _shutting_down


class ShutdownAwareMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _shutting_down and request.url.path not in SHUTDOWN_WHITELIST:
            return JSONResponse(status_code=503, content={"detail": "Service shutting down"})
        return await call_next(request)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_shutdown_aware_middleware.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/middleware/shutdown_aware.py apps/backend/tests/unit/core/test_shutdown_aware_middleware.py
git commit -m "feat(backend): shutdown-aware 503 middleware + process flag"
```

---

### Task 2: Graceful RPC drain in `GatewayConnection.close()` + parallel `close_all`

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`close` ~1326-1367; `close_all` ~1673-1676; add `_RPC_DRAIN_TIMEOUT` near `_RPC_TIMEOUT` ~29)
- Test: `apps/backend/tests/unit/core/test_connection_pool_close_drain.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/core/test_connection_pool_close_drain.py
import asyncio
import pytest

from core.gateway import connection_pool as cp


class _FakeWS:
    def __init__(self):
        self.closed = False
    async def close(self):
        self.closed = True


def _make_conn():
    conn = cp.GatewayConnection.__new__(cp.GatewayConnection)
    conn.user_id = "u1"
    conn._closed = False
    conn._ws = _FakeWS()
    conn._pending_rpcs = {}
    conn._billing_tasks = set()
    conn._grace_task = None
    conn._parked_watchdog_task = None
    conn._reader_task = None
    conn._active_runs = {}
    conn._parked = False
    conn._emit_status_change = lambda *a, **k: None
    return conn


@pytest.mark.asyncio
async def test_inflight_rpc_resolved_during_drain_is_not_rejected():
    conn = _make_conn()
    fut = asyncio.get_event_loop().create_future()
    conn._pending_rpcs["r1"] = fut
    # Resolve the future shortly after close() starts draining.
    async def _resolve():
        await asyncio.sleep(0.05)
        if not fut.done():
            fut.set_result({"ok": True})
    asyncio.create_task(_resolve())
    await conn.close()
    assert fut.done() and fut.exception() is None
    assert conn._ws.closed is True


@pytest.mark.asyncio
async def test_unresolved_rpc_rejected_after_drain_timeout(monkeypatch):
    monkeypatch.setattr(cp, "_RPC_DRAIN_TIMEOUT", 0.1)
    conn = _make_conn()
    fut = asyncio.get_event_loop().create_future()
    conn._pending_rpcs["r1"] = fut
    await conn.close()
    assert fut.done() and isinstance(fut.exception(), RuntimeError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_connection_pool_close_drain.py -v`
Expected: FAIL — first test fails because current `close()` rejects pending RPCs after WS close instead of awaiting them (or `_RPC_DRAIN_TIMEOUT` undefined).

- [ ] **Step 3: Write minimal implementation**

Add the constant near `_RPC_TIMEOUT` (~line 29):

```python
_RPC_DRAIN_TIMEOUT = 5.0  # graceful-shutdown: wait this long for in-flight RPCs to settle before closing the WS
```

Replace the body of `GatewayConnection.close()` (lines 1326-1367) — insert the drain **before** cancelling the reader / closing the WS:

```python
    async def close(self) -> None:
        """Shut down: drain in-flight RPCs, cancel reader, close WebSocket."""
        self._emit_status_change("GATEWAY_DOWN_DEAD", "Gateway connection closed")
        self._closed = True
        # Wait for in-flight billing-resolver tasks so teardown doesn't drop usage events.
        if self._billing_tasks:
            try:
                await asyncio.wait(self._billing_tasks, timeout=5.0)
            except Exception:
                logger.exception("Error waiting for billing tasks during close for user %s", self.user_id)
        # Graceful RPC drain: the reader loop is still alive here, so in-flight
        # RPCs can still receive their `res` and resolve. Wait (bounded) for the
        # pending set to settle BEFORE tearing down the transport, instead of
        # rejecting them after WS close (which makes callers hang to _RPC_TIMEOUT).
        pending = [f for f in self._pending_rpcs.values() if not f.done()]
        if pending:
            try:
                await asyncio.wait(pending, timeout=_RPC_DRAIN_TIMEOUT)
            except Exception:
                logger.exception("Error draining pending RPCs during close for user %s", self.user_id)
        if self._grace_task and not self._grace_task.done():
            self._grace_task.cancel()
        if self._parked_watchdog_task and not self._parked_watchdog_task.done():
            self._parked_watchdog_task.cancel()
            try:
                await self._parked_watchdog_task
            except asyncio.CancelledError:
                pass
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        # Reject any RPCs that did NOT settle during the drain window.
        for req_id, future in list(self._pending_rpcs.items()):
            if not future.done():
                future.set_exception(RuntimeError("Connection closed"))
        self._pending_rpcs.clear()
        self._active_runs.clear()
        self._parked = False
```

Replace `close_all` (lines 1673-1676) to close users concurrently so total drain is bounded by one `_RPC_DRAIN_TIMEOUT`, not N×:

```python
    async def close_all(self) -> None:
        """Shutdown: close all connections concurrently."""
        await asyncio.gather(
            *(self.close_user(uid) for uid in list(self._connections.keys())),
            return_exceptions=True,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_connection_pool_close_drain.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/core/test_connection_pool_close_drain.py
git commit -m "feat(gateway): drain in-flight RPCs before WS close; close_all concurrently"
```

---

### Task 3: Bounded Teams broker shutdown

**Files:**
- Modify: `apps/backend/core/services/teams_event_broker.py` (`shutdown` ~266-282)
- Test: `apps/backend/tests/unit/services/test_teams_event_broker_shutdown_timeout.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_teams_event_broker_shutdown_timeout.py
import asyncio
import pytest

from core.services import teams_event_broker as teb


class _HangingClient:
    def __init__(self):
        self.close_started = False
    async def close(self):
        self.close_started = True
        await asyncio.sleep(3600)  # never returns


class _FastClient:
    def __init__(self):
        self.closed = False
    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_hanging_client_does_not_block_shutdown(monkeypatch):
    monkeypatch.setattr(teb, "_CLIENT_CLOSE_TIMEOUT", 0.1)
    broker = teb.TeamsEventBroker.__new__(teb.TeamsEventBroker)
    broker._grace_tasks = {}
    hanging, fast = _HangingClient(), _FastClient()
    broker._clients = {"a": hanging, "b": fast}
    broker._subscribers = {}
    await asyncio.wait_for(broker.shutdown(), timeout=2.0)  # must not hang
    assert hanging.close_started is True
    assert fast.closed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_teams_event_broker_shutdown_timeout.py -v`
Expected: FAIL — `asyncio.wait_for(... 2.0)` times out (current `shutdown` awaits `client.close()` unbounded), or `_CLIENT_CLOSE_TIMEOUT` undefined.

- [ ] **Step 3: Write minimal implementation**

Add a module constant near the top of `teams_event_broker.py`:

```python
_CLIENT_CLOSE_TIMEOUT = 3.0  # graceful-shutdown: bound each backing client.close()
```

Replace `shutdown` (lines 266-282):

```python
    async def shutdown(self) -> None:
        """Close every backing client + cancel grace tasks, each bounded so a
        stalled Paperclip upstream cannot block lifespan shutdown into SIGKILL."""
        grace_tasks = list(self._grace_tasks.values())
        for task in grace_tasks:
            task.cancel()
        self._grace_tasks.clear()
        if grace_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*grace_tasks, return_exceptions=True),
                    timeout=_CLIENT_CLOSE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("teams broker shutdown: grace-task cancellation timed out")
        clients = list(self._clients.values())
        self._clients.clear()
        self._subscribers.clear()
        for client in clients:
            try:
                await asyncio.wait_for(client.close(), timeout=_CLIENT_CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("teams broker shutdown: client.close() timed out, abandoning")
            except Exception:
                logger.exception("teams broker shutdown: close() failed")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_teams_event_broker_shutdown_timeout.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/teams_event_broker.py apps/backend/tests/unit/services/test_teams_event_broker_shutdown_timeout.py
git commit -m "feat(teams): bound broker shutdown client.close() so a hung upstream can't block SIGTERM"
```

---

### Task 4: Graceful scheduled-worker stop (event, not hard cancel)

**Files:**
- Modify: `apps/backend/core/services/update_service.py` (`run_scheduled_worker` ~366-412)
- Test: `apps/backend/tests/test_update_service_graceful_stop.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/test_update_service_graceful_stop.py
import asyncio
import pytest

from core.services import update_service as us


@pytest.mark.asyncio
async def test_worker_exits_cleanly_on_stop_without_cancel(monkeypatch):
    iterations = {"n": 0}

    async def _fake_pass():
        iterations["n"] += 1

    # Make each loop iteration cheap + the inter-iteration wait short.
    monkeypatch.setattr(us, "_paperclip_provision_retry_pass", _fake_pass, raising=False)
    monkeypatch.setattr(us, "_WORKER_INTERVAL_S", 0.05, raising=False)

    us.reset_worker_stop()
    task = asyncio.create_task(us.run_scheduled_worker())
    await asyncio.sleep(0.12)
    us.request_worker_stop()
    # Must finish on its own (no .cancel()) within a bounded wait.
    await asyncio.wait_for(task, timeout=1.0)
    assert iterations["n"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_update_service_graceful_stop.py -v`
Expected: FAIL — `reset_worker_stop`/`request_worker_stop`/`_WORKER_INTERVAL_S` don't exist; `run_scheduled_worker` loops forever (`asyncio.wait_for` times out).

- [ ] **Step 3: Write minimal implementation**

Near the top of `update_service.py`:

```python
import asyncio

_WORKER_INTERVAL_S = 60
_worker_stop = asyncio.Event()


def request_worker_stop() -> None:
    """Ask run_scheduled_worker to finish its current iteration and exit."""
    _worker_stop.set()


def reset_worker_stop() -> None:
    """Reset the stop signal (start-of-run / test isolation)."""
    _worker_stop.clear()
```

Change `run_scheduled_worker` (the `while True` at ~375 and the `asyncio.sleep(60)` at ~412):

```python
async def run_scheduled_worker() -> None:
    reset_worker_stop()
    while not _worker_stop.is_set():
        # ... existing per-iteration body unchanged (provision retry pass, purge, etc.) ...

        # Interruptible inter-iteration wait: wakes immediately on stop request.
        try:
            await asyncio.wait_for(_worker_stop.wait(), timeout=_WORKER_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
```

> Keep the existing loop body exactly as-is; only the loop condition and the final `await asyncio.sleep(60)` change. The gauge loop stays a hard `.cancel()` (it's stateless metric emission — acceptable per grounding).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_update_service_graceful_stop.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/update_service.py apps/backend/tests/test_update_service_graceful_stop.py
git commit -m "feat(update-service): graceful worker stop via event (finish current iteration)"
```

---

### Task 5: Bounded, ordered lifespan shutdown + wire middleware + uvicorn flag

**Files:**
- Modify: `apps/backend/main.py` (lifespan shutdown ~329-349; middleware add after `RequestContextMiddleware` ~441)
- Modify: `apps/backend/Dockerfile` (CMD line 63)
- Test: `apps/backend/tests/unit/test_main_lifespan_shutdown.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/test_main_lifespan_shutdown.py
import asyncio
import pytest


@pytest.mark.asyncio
async def test_lifespan_shutdown_sets_flag_and_is_bounded(monkeypatch):
    import main
    from core.middleware import shutdown_aware

    shutdown_aware.set_shutting_down(False)

    # Stub the heavy startup/shutdown collaborators.
    monkeypatch.setattr(main, "startup_containers", lambda: asyncio.sleep(0))
    monkeypatch.setattr(main, "_resume_provisioning_transitions", lambda: asyncio.sleep(0))
    monkeypatch.setattr(main, "run_scheduled_worker", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(main, "_running_count_gauge_loop", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(main, "_build_teams_event_broker", lambda: asyncio.sleep(0, result=None))

    flag_during_shutdown = {}

    async def _spy_shutdown_containers():
        flag_during_shutdown["set"] = shutdown_aware.is_shutting_down()
    monkeypatch.setattr(main, "shutdown_containers", _spy_shutdown_containers)

    async with main.lifespan(main.app):
        assert shutdown_aware.is_shutting_down() is False
    # After exit, shutdown ran: flag was set, and shutdown_containers saw it.
    assert flag_during_shutdown.get("set") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_main_lifespan_shutdown.py -v`
Expected: FAIL — shutdown does not set `_shutting_down`, so `flag_during_shutdown["set"]` is False/missing.

- [ ] **Step 3: Write minimal implementation**

Add the middleware wiring after the `RequestContextMiddleware` add (~line 441):

```python
from core.middleware.shutdown_aware import ShutdownAwareMiddleware, set_shutting_down

app.add_middleware(ShutdownAwareMiddleware)
```

Replace the lifespan **shutdown** block (lines 329-349) with a bounded, ordered drain:

```python
    # Shutdown — bounded so no step can ride into the ECS SIGKILL window.
    # Ladder: this timeout(20) < uvicorn --timeout-graceful-shutdown 25 < ECS stopTimeout 30.
    logger.info("Shutting down application...")
    set_shutting_down(True)  # 503 new business requests; /health stays green for ALB drain
    from core.services.update_service import request_worker_stop

    try:
        async with asyncio.timeout(20):
            system_health.BACKGROUND_TASKS.clear()
            # Worker: graceful stop (finish current iteration); gauge: stateless hard-cancel.
            request_worker_stop()
            gauge_task.cancel()
            try:
                await gauge_task
            except asyncio.CancelledError:
                pass
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
            if broker is not None:
                from core.services import teams_event_broker_singleton

                await broker.shutdown()
                teams_event_broker_singleton.set_broker(None)
            await shutdown_containers()
    except asyncio.TimeoutError:
        logger.error("Graceful shutdown exceeded 20s budget; exiting to let the platform reclaim the task")
```

Update the Dockerfile CMD (line 63) to give uvicorn a graceful window longer than the app budget but shorter than ECS `stopTimeout`:

```dockerfile
CMD ["sh", "-c", "uv run python init_db.py 2>&1 || echo 'WARNING: init_db failed'; exec uv run uvicorn main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 300 --timeout-graceful-shutdown 25"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/test_main_lifespan_shutdown.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/main.py apps/backend/Dockerfile apps/backend/tests/unit/test_main_lifespan_shutdown.py
git commit -m "feat(backend): bounded ordered graceful shutdown + wire shutdown-aware middleware + uvicorn grace window"
```

---

### Task 6: Cheap liveness health check (no external dependency)

**Files:**
- Modify: `apps/backend/main.py` (lifespan startup: set `_started`; `health_check` ~686-696)
- Test: `apps/backend/tests/unit/test_health_check_liveness.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/test_health_check_liveness.py
import pytest
from fastapi.testclient import TestClient


def test_health_does_not_touch_dynamodb(monkeypatch):
    import main

    called = {"ddb": False}

    def _boom(*a, **k):
        called["ddb"] = True
        raise AssertionError("health must not call DynamoDB")

    monkeypatch.setattr(main, "get_table", _boom, raising=False)
    main._started = True
    client = TestClient(main.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert called["ddb"] is False


def test_health_503_before_startup_complete():
    import main

    main._started = False
    client = TestClient(main.app)
    assert client.get("/health").status_code == 503
    main._started = True  # restore
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_health_check_liveness.py -v`
Expected: FAIL — current health calls DynamoDB (`get_table("users").load()`), so `test_health_does_not_touch_dynamodb` trips the `_boom`.

- [ ] **Step 3: Write minimal implementation**

Add a module-level flag near the top of `main.py` and set it at the END of the lifespan **startup** block (just before `yield`):

```python
_started = False
```

```python
    # ... end of startup, just before `yield` ...
    global _started
    _started = True
    yield
```

Replace `health_check` (lines 686-696) with a cheap, local liveness check (no external calls — avoids ALB flap when DynamoDB is slow):

```python
async def health_check():
    """ALB liveness — cheap + local. 200 once startup completed; 503 while still
    booting so ALB doesn't route to a not-ready task. Deliberately does NOT call
    DynamoDB or any upstream (that would tie liveness to external availability and
    flap the task)."""
    if not _started:
        return JSONResponse(status_code=503, content={"status": "starting"})
    return {"status": "healthy"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/test_health_check_liveness.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/main.py apps/backend/tests/unit/test_health_check_liveness.py
git commit -m "feat(backend): cheap local liveness health check (no DynamoDB dependency)"
```

---

### Task 7: Graceful container-resolution error (no hang on a starting container)

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`send_rpc` resolve path ~1507-1559) + a new exception
- Modify: `apps/backend/routers/container_rpc.py` (map the new exception → 503) — confirm exact router during impl
- Test: `apps/backend/tests/unit/core/test_container_resolution_error.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/core/test_container_resolution_error.py
import pytest

from core.gateway import connection_pool as cp


@pytest.mark.asyncio
async def test_resolve_failure_raises_container_not_ready(monkeypatch):
    pool = cp.GatewayConnectionPool.__new__(cp.GatewayConnectionPool)
    pool._connections = {}

    async def _fail_resolve(*a, **k):
        raise RuntimeError("no running task")

    # The resolution helper the pool uses before opening a connection.
    monkeypatch.setattr(pool, "_resolve_container_ip", _fail_resolve, raising=False)

    with pytest.raises(cp.ContainerNotReadyError):
        await pool.send_rpc("owner_1", "agents.list", {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_container_resolution_error.py -v`
Expected: FAIL — `ContainerNotReadyError` doesn't exist; resolution failure currently raises a raw `RuntimeError`/`ClientError`.

> Implementation note for the worker: read `send_rpc` (~1507-1559) to find the exact resolution call (e.g. `ecs.resolve_running_container(...)` / `_resolve_container_ip`). Wrap **only** the resolution step; do not change the happy path.

- [ ] **Step 3: Write minimal implementation**

Add near the top of `connection_pool.py`:

```python
class ContainerNotReadyError(Exception):
    """Raised when the user's OpenClaw container can't be resolved yet (starting/replacing).
    Callers should surface a structured 503 'container starting' rather than hang or 500."""
```

In `send_rpc`, wrap the resolution step so a failure becomes a typed, fast error:

```python
        try:
            ip = await self._resolve_container_ip(owner_id)  # exact call per current code
        except ContainerNotReadyError:
            raise
        except Exception as e:
            raise ContainerNotReadyError(f"container for {owner_id} not ready: {e}") from e
```

In the RPC router, catch it and return 503 (confirm file — likely `routers/container_rpc.py`):

```python
    from core.gateway.connection_pool import ContainerNotReadyError
    try:
        result = await pool.send_rpc(owner_id, method, params)
    except ContainerNotReadyError:
        return JSONResponse(status_code=503, content={"detail": "container starting", "retryable": True})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_container_resolution_error.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/routers/container_rpc.py apps/backend/tests/unit/core/test_container_resolution_error.py
git commit -m "feat(gateway): typed ContainerNotReadyError -> 503 instead of hanging on a starting container"
```

---

### Task 8: CDK — rolling deploy + drain + stop-timeout config (+ assertions)

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts` (FargateService props + backend container `stopTimeout`)
- Modify: `apps/infra/lib/stacks/network-stack.ts` (target group `deregistrationDelay`)
- Test: `apps/infra/test/service-stack.test.ts`, `apps/infra/test/network-stack.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// apps/infra/test/service-stack.test.ts
import { Template } from "aws-cdk-lib/assertions";
import { App } from "aws-cdk-lib";
import { synthForTest } from "./helpers"; // reuse the existing stage/synth helper used by container-stack.test.ts

test("backend service uses rolling deploy that starts new before stopping old", () => {
  const template = synthForTest("service"); // returns Template for ServiceStack
  template.hasResourceProperties("AWS::ECS::Service", {
    DeploymentConfiguration: {
      MinimumHealthyPercent: 100,
      MaximumPercent: 200,
      DeploymentCircuitBreaker: { Enable: true, Rollback: true },
    },
  });
});

test("backend container has a bounded stopTimeout", () => {
  const template = synthForTest("service");
  template.hasResourceProperties("AWS::ECS::TaskDefinition", {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({ StopTimeout: 30 }),
    ]),
  });
});
```

```typescript
// apps/infra/test/network-stack.test.ts
import { Template } from "aws-cdk-lib/assertions";
import { synthForTest } from "./helpers";

test("backend target group drains quickly (deregistration delay)", () => {
  const template = synthForTest("network");
  template.hasResourceProperties("AWS::ElasticLoadBalancingV2::TargetGroup", {
    TargetGroupAttributes: Match.arrayWith([
      { Key: "deregistration_delay.timeout_seconds", Value: "25" },
    ]),
  });
});
```

> First read `apps/infra/test/container-stack.test.ts` to copy its exact synth/`Template.fromStack` helper pattern (the import for `Match`, how a single stack is synthesized in isolation). Mirror it in a small `apps/infra/test/helpers.ts` if one doesn't already exist.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/infra && pnpm test -- service-stack network-stack`
Expected: FAIL — current service has no `MinimumHealthyPercent/MaximumPercent` set the way asserted, no `StopTimeout`, target group has no explicit `deregistration_delay`.

- [ ] **Step 3: Write minimal implementation**

In `service-stack.ts`, on the backend `FargateService` props (~line 921, alongside `desiredCount`/`circuitBreaker`):

```typescript
      desiredCount: 1,
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      circuitBreaker: { rollback: true },
```

On the backend container definition (`taskDefinition.addContainer({...})`):

```typescript
      stopTimeout: cdk.Duration.seconds(30), // SIGTERM->SIGKILL window > uvicorn(25) > app(20)
```

In `network-stack.ts`, on the backend `ApplicationTargetGroup` (~line 86-100):

```typescript
      deregistrationDelay: cdk.Duration.seconds(25), // drain in-flight before kill; <= stopTimeout
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/infra && pnpm test -- service-stack network-stack`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/service-stack.ts apps/infra/lib/stacks/network-stack.ts apps/infra/test/service-stack.test.ts apps/infra/test/network-stack.test.ts apps/infra/test/helpers.ts
git commit -m "feat(infra): rolling deploy (min100/max200) + bounded stopTimeout + fast target-group drain"
```

---

## Final verification

- [ ] Backend suite: `cd apps/backend && uv run pytest tests/ -q` — all green.
- [ ] Infra suite: `cd apps/infra && pnpm test` — all green.
- [ ] Manual (dev): deploy and watch a chat session stay alive across the rolling handoff (new task healthy before old drains); then stop a task and confirm graceful drain in logs (no SIGKILL-mid-cleanup). Per repo norms, push only after approval; watch the CI run.

## Out of scope (tracked elsewhere)
- DDB-backed routing (Piece 1 / Plan 2) and ownership lease (Piece 2 / Plan 3) — the actual N>1 enablers.
- RPC idempotency keys — deferred to the scaling plans (only matters at N>1).
- Dead-air `run_active` UX fix — issue #780, ships after the scaling work.
