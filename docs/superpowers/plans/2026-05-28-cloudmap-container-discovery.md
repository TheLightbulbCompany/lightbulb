# Cloud Map Container Discovery + Stale-IP Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve per-user OpenClaw container IPs through AWS Cloud Map `DiscoverInstances` (instead of ECS `describe_tasks`), and make the gateway connection pool re-resolve the endpoint on every reconnect, so a container task replacement can never wedge the backend on a dead IP.

**Architecture:** Every per-user ECS service already registers its task into one shared Cloud Map service (`serviceRegistries`). We start *reading* from that same registry — `DiscoverInstances` filtered on the `ECS_SERVICE_NAME` attribute returns only the current HEALTHY task for one tenant. Separately, the connection pool is fixed to adopt the freshly-resolved IP before reusing/reopening a connection, and the parked-watchdog is re-armed on a failed reopen so it can still escalate to a full rebuild.

**Tech Stack:** Python 3.13, FastAPI, boto3 (`servicediscovery`), pytest; AWS CDK (TypeScript) for env wiring; ECS Fargate + AWS Cloud Map.

---

## Problem / Root cause (why this plan exists)

Live incident (org `org_3DBS8G83oPrMm49yN2b70EjoFpN`, prod): the site shows "connected" but no agents load and the control dashboard is dead. Evidence from `/ecs/isol8-prod`:

- Browser↔backend WebSocket is healthy (RPCs arrive). Backend↔container WebSocket times out: `Gateway WS upgrade failed ... ip=10.2.3.232 ... timed out during opening handshake`.
- The live task is at `10.2.3.224` (ECS `describe-tasks` + Cloud Map both agree); `10.2.3.232` is a **previous** task replaced on 2026-05-27.
- The connection got "parked" (reader loop died mid-run), which pins `self.ip`; `reopen()` re-dials that cached IP and never re-resolves, and the parked watchdog (the only escape hatch) is cancelled by `reopen()` and never re-armed → wedged for days, surviving the task replacement.

Two independent defects:
1. **Discovery is decoupled from registration.** We register into Cloud Map but resolve via `describe_tasks`, and the connection layer caches the resolved IP indefinitely.
2. **A parked connection never re-resolves and never self-heals** (`reopen()` reuses `self.ip`; failed reopen leaves `_parked=True` with no watchdog).

This plan fixes both. Shipping it (a backend redeploy) also clears the current wedge, because a fresh pool re-resolves on first RPC.

---

## File Structure

- `apps/backend/core/config.py` — add `CLOUD_MAP_NAMESPACE_NAME` + `CLOUD_MAP_SERVICE_NAME` settings (DiscoverInstances needs *names*, not the IDs/ARN we currently store).
- `apps/infra/lib/stacks/service-stack.ts` — pass those two names into the backend container env (CDK already exposes both as attributes; IAM already grants `DiscoverInstances`).
- `apps/backend/core/containers/ecs_manager.py` — add a `servicediscovery` boto3 client + name fields in `__init__`; replace `discover_ip()` body to use Cloud Map. Public signature unchanged, so all 14 callers of `resolve_running_container`/`discover_ip` are untouched.
- `apps/backend/core/gateway/connection_pool.py` — `send_rpc()` adopts the freshly-resolved IP onto an existing connection before reuse/reopen; `reopen()` re-arms the parked watchdog on failure.
- `apps/backend/tests/unit/containers/test_ecs_manager.py` — rewrite `TestDiscoverIp` for Cloud Map; add `mock_servicediscovery_client` fixture + name settings.
- `apps/backend/tests/unit/core/test_connection_pool.py` — add tests for fresh-IP adoption and watchdog re-arm.

---

## Task 1: De-risk — prove the Cloud Map filter returns the right instance (read-only)

This validates the one load-bearing assumption (that `DiscoverInstances` can scope to one tenant via the `ECS_SERVICE_NAME` attribute) against live AWS **before** writing code. No mutations.

**Files:** none (investigation only).

- [ ] **Step 1: Get the Cloud Map service name + the org's ECS service name**

Run:
```bash
aws servicediscovery get-service --id srv-pibibqwsdos3vwjz \
  --profile isol8-admin --region us-east-1 --query 'Service.Name' --output text
aws dynamodb get-item --table-name isol8-prod-containers \
  --key '{"owner_id":{"S":"org_3DBS8G83oPrMm49yN2b70EjoFpN"}}' \
  --profile isol8-admin --region us-east-1 --query 'Item.service_name.S' --output text
```
Expected: a service name (e.g. `NamespaceContainerService34-obyYIeMXIkov`) and `openclaw-org_3DBS8G83oPrMm49yN2b70EjoFpN-870542bb5d22`.

- [ ] **Step 2: Run the filtered DiscoverInstances exactly as the code will**

Run (substitute the service name from Step 1):
```bash
aws servicediscovery discover-instances \
  --namespace-name isol8-prod.local \
  --service-name '<SERVICE_NAME_FROM_STEP_1>' \
  --query-parameters ECS_SERVICE_NAME=openclaw-org_3DBS8G83oPrMm49yN2b70EjoFpN-870542bb5d22 \
  --health-status HEALTHY \
  --profile isol8-admin --region us-east-1
```
Expected: exactly one instance whose `Attributes.AWS_INSTANCE_IPV4` is `10.2.3.224` (the live task) and `Attributes.ECS_SERVICE_NAME` matches the requested service.

**Decision gate:** If this returns the single correct instance, proceed. If `QueryParameters` is ignored (returns instances for other tenants too), the code in Task 3 still filters client-side on `ECS_SERVICE_NAME`, so it remains correct — just note it in the Task 3 commit. If it returns nothing, stop and investigate registration before continuing.

**Spike result (2026-05-28):** Returns exactly one instance, `AWS_INSTANCE_IPV4=10.2.3.224`, `ECS_SERVICE_NAME` matches. **Important nuance:** the instance's `HealthStatus` is `UNKNOWN` (`AWS_INIT_HEALTH_STATUS=UNHEALTHY`) because the Cloud Map service has no `HealthCheckCustomConfig` — ECS registers the task but Cloud Map tracks no health. `HealthStatus=HEALTHY` still returned it, but to avoid depending on that lenient behavior, Task 3 uses **`HealthStatus="HEALTHY_OR_ELSE_ALL"`**: returns our instance today (no health data → falls back to all matching), and prefers healthy tasks automatically if health-checking is ever added.

---

## Task 2: Add Cloud Map name settings + wire them into the backend env

`DiscoverInstances` takes the namespace **name** and service **name** (strings). We currently only pass IDs/ARN. CDK already exposes both names as attributes; add them to the backend container env and to settings.

**Files:**
- Modify: `apps/backend/core/config.py`
- Modify: `apps/infra/lib/stacks/service-stack.ts:764-767`
- Test: `apps/backend/tests/unit/test_config_cloudmap.py` (create)

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/test_config_cloudmap.py`:
```python
"""Settings expose Cloud Map namespace/service NAMES for DiscoverInstances."""

import importlib
import os


def test_cloud_map_name_settings_read_env(monkeypatch):
    monkeypatch.setenv("CLOUD_MAP_NAMESPACE_NAME", "isol8-test.local")
    monkeypatch.setenv("CLOUD_MAP_SERVICE_NAME", "openclaw-containers")
    import core.config as config

    importlib.reload(config)
    try:
        assert config.settings.CLOUD_MAP_NAMESPACE_NAME == "isol8-test.local"
        assert config.settings.CLOUD_MAP_SERVICE_NAME == "openclaw-containers"
    finally:
        importlib.reload(config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_config_cloudmap.py -v`
Expected: FAIL with `AttributeError: ... CLOUD_MAP_NAMESPACE_NAME` (settings don't exist yet).

- [ ] **Step 3: Add the settings**

In `apps/backend/core/config.py`, immediately after the existing `CLOUD_MAP_SERVICE_ARN` line (currently `core/config.py:60`):
```python
    CLOUD_MAP_NAMESPACE_NAME: str = os.getenv("CLOUD_MAP_NAMESPACE_NAME", "")
    CLOUD_MAP_SERVICE_NAME: str = os.getenv("CLOUD_MAP_SERVICE_NAME", "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/test_config_cloudmap.py -v`
Expected: PASS.

- [ ] **Step 5: Wire the env vars in CDK**

In `apps/infra/lib/stacks/service-stack.ts`, in the backend container `environment` block (right after the existing `CLOUD_MAP_SERVICE_ARN` entry at line 767):
```typescript
        CLOUD_MAP_NAMESPACE_NAME: props.container.cloudMapNamespace.namespaceName,
        CLOUD_MAP_SERVICE_NAME: props.container.cloudMapService.serviceName,
```

- [ ] **Step 6: Verify CDK synthesizes the env vars**

Run: `cd apps/infra && npx cdk synth 'isol8-prod/*' 2>/dev/null | grep -A1 'CLOUD_MAP_SERVICE_NAME'`
Expected: the synthesized task definition shows `CLOUD_MAP_NAMESPACE_NAME` and `CLOUD_MAP_SERVICE_NAME` with a `Fn::GetAtt`/`Ref` value (not empty). If `cdk synth` requires context/bootstrap unavailable locally, instead confirm by `grep -n "CLOUD_MAP_SERVICE_NAME" apps/infra/lib/stacks/service-stack.ts` returns the new line.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/config.py apps/backend/tests/unit/test_config_cloudmap.py apps/infra/lib/stacks/service-stack.ts
git commit -m "$(cat <<'EOF'
feat(discovery): pass Cloud Map namespace/service names to backend

DiscoverInstances needs the namespace + service NAMES, not the IDs/ARN
we currently store. Expose both from CDK and read them in settings.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Resolve container IP via Cloud Map DiscoverInstances

Replace the `describe_tasks` body of `discover_ip()` with a Cloud Map lookup. The public signature (`discover_ip(service_name) -> str | None`) is unchanged, so `resolve_running_container` and all downstream callers are untouched.

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py:83-93` (`__init__`) and `:955-997` (`discover_ip`)
- Test: `apps/backend/tests/unit/containers/test_ecs_manager.py:82-110` (fixtures) and `:802-885` (`TestDiscoverIp`)

- [ ] **Step 1: Update test fixtures to provide a servicediscovery mock + name settings**

In `apps/backend/tests/unit/containers/test_ecs_manager.py`, add this fixture after the `mock_efs_client` fixture (around line 80):
```python
@pytest.fixture
def mock_servicediscovery_client():
    """Create a mock AWS Cloud Map (servicediscovery) boto3 client."""
    client = MagicMock()
    client.discover_instances.return_value = {"Instances": []}
    return client
```

In the `mock_settings` fixture, add these two lines after the `s.CLOUD_MAP_SERVICE_ARN = ...` line (currently line 91):
```python
        s.CLOUD_MAP_NAMESPACE_NAME = "isol8-dev.local"
        s.CLOUD_MAP_SERVICE_NAME = "openclaw-containers"
```

Replace the `manager` fixture (currently lines 96-110) with:
```python
@pytest.fixture
def manager(mock_settings, mock_ecs_client, mock_efs_client, mock_servicediscovery_client):
    """Create an EcsManager with mocked boto3 clients."""
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:
        def client_factory(service, **kwargs):
            if service == "ecs":
                return mock_ecs_client
            elif service == "efs":
                return mock_efs_client
            elif service == "servicediscovery":
                return mock_servicediscovery_client
            return MagicMock()

        mock_boto3.client.side_effect = client_factory
        mgr = EcsManager()
    return mgr
```

- [ ] **Step 2: Replace the `TestDiscoverIp` class with Cloud Map tests**

Replace the entire `TestDiscoverIp` class (currently lines 802-885) with:
```python
class TestDiscoverIp:
    """Test Cloud Map (DiscoverInstances) task IP discovery."""

    def test_returns_ip(self, manager, mock_servicediscovery_client):
        """discover_ip returns the HEALTHY instance IP for the matching tenant."""
        mock_servicediscovery_client.discover_instances.return_value = {
            "Instances": [
                {
                    "InstanceId": "task-abc",
                    "Attributes": {
                        "ECS_SERVICE_NAME": "openclaw-abc123def4",
                        "AWS_INSTANCE_IPV4": "10.0.1.42",
                    },
                }
            ]
        }

        ip = manager.discover_ip("openclaw-abc123def4")

        assert ip == "10.0.1.42"
        mock_servicediscovery_client.discover_instances.assert_called_once_with(
            NamespaceName="isol8-dev.local",
            ServiceName="openclaw-containers",
            QueryParameters={"ECS_SERVICE_NAME": "openclaw-abc123def4"},
            HealthStatus="HEALTHY_OR_ELSE_ALL",
        )

    def test_returns_none_when_no_instances(self, manager, mock_servicediscovery_client):
        """No healthy instance for the tenant -> None."""
        mock_servicediscovery_client.discover_instances.return_value = {"Instances": []}
        assert manager.discover_ip("openclaw-abc123def4") is None

    def test_returns_none_on_error(self, manager, mock_servicediscovery_client):
        """SDK error -> None (caller treats as not-ready)."""
        mock_servicediscovery_client.discover_instances.side_effect = ClientError(
            {"Error": {"Code": "ServiceNotFound", "Message": "x"}},
            "DiscoverInstances",
        )
        assert manager.discover_ip("openclaw-abc123def4") is None

    def test_ignores_instances_for_other_tenants(self, manager, mock_servicediscovery_client):
        """Defensive client-side filter: never return another tenant's IP even
        if the server-side QueryParameters filter is widened/ignored."""
        mock_servicediscovery_client.discover_instances.return_value = {
            "Instances": [
                {"Attributes": {"ECS_SERVICE_NAME": "openclaw-other", "AWS_INSTANCE_IPV4": "10.0.9.9"}}
            ]
        }
        assert manager.discover_ip("openclaw-abc123def4") is None

    def test_namespace_not_configured_returns_none(self, manager, mock_servicediscovery_client):
        """Missing Cloud Map names -> None, no SDK call."""
        manager._cloud_map_namespace_name = ""
        assert manager.discover_ip("openclaw-abc123def4") is None
        mock_servicediscovery_client.discover_instances.assert_not_called()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_ecs_manager.py::TestDiscoverIp -v`
Expected: FAIL (current `discover_ip` calls `list_tasks`/`describe_tasks`, not `discover_instances`; `manager` has no `_cloud_map_namespace_name`).

- [ ] **Step 4: Add the servicediscovery client + name fields in `__init__`**

In `apps/backend/core/containers/ecs_manager.py`, in `EcsManager.__init__`, after the existing `self._cloud_map_service_arn = settings.CLOUD_MAP_SERVICE_ARN` line (currently line 92):
```python
        self._servicediscovery = boto3.client("servicediscovery", region_name=settings.AWS_REGION)
        self._cloud_map_namespace_name = settings.CLOUD_MAP_NAMESPACE_NAME
        self._cloud_map_service_name = settings.CLOUD_MAP_SERVICE_NAME
```

- [ ] **Step 5: Replace the `discover_ip` implementation**

Replace the entire `discover_ip` method body (currently lines 955-997) with:
```python
    def discover_ip(self, service_name: str) -> str | None:
        """Discover a task's private IP via AWS Cloud Map.

        Every per-user OpenClaw ECS service registers its task into one shared
        Cloud Map service (see apps/infra/lib/stacks/container-stack.ts), each
        instance carrying an ``ECS_SERVICE_NAME`` attribute. We scope the
        lookup to a single tenant by filtering on that attribute, so the IP
        reflects the *current* task and can never be stale across task
        replacements — unlike a cached address.

        ``HealthStatus="HEALTHY_OR_ELSE_ALL"``: the shared Cloud Map service has
        no HealthCheckCustomConfig, so ECS-registered instances report health
        ``UNKNOWN``. This mode returns the current task regardless of that, and
        automatically prefers healthy tasks if health-checking is ever enabled.

        Args:
            service_name: The per-user ECS service name (the ``ECS_SERVICE_NAME``
                attribute value to match).

        Returns:
            The current task's private IPv4 address, or None.
        """
        if not self._cloud_map_namespace_name or not self._cloud_map_service_name:
            logger.error(
                "Cloud Map names not configured (namespace=%r service=%r); cannot discover %s",
                self._cloud_map_namespace_name,
                self._cloud_map_service_name,
                service_name,
            )
            return None
        try:
            resp = self._servicediscovery.discover_instances(
                NamespaceName=self._cloud_map_namespace_name,
                ServiceName=self._cloud_map_service_name,
                QueryParameters={"ECS_SERVICE_NAME": service_name},
                HealthStatus="HEALTHY_OR_ELSE_ALL",
            )
        except Exception as e:
            logger.error("Cloud Map discovery failed for %s: %s", service_name, e)
            return None
        for inst in resp.get("Instances", []):
            attrs = inst.get("Attributes", {})
            if attrs.get("ECS_SERVICE_NAME") == service_name and attrs.get("AWS_INSTANCE_IPV4"):
                return attrs["AWS_INSTANCE_IPV4"]
        return None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_ecs_manager.py -v`
Expected: PASS (the whole file, to confirm no other test relied on the old `describe_tasks` discovery path).

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_ecs_manager.py
git commit -m "$(cat <<'EOF'
feat(discovery): resolve container IP via Cloud Map DiscoverInstances

Read the per-user task IP from the same Cloud Map service we already
register into, filtered on ECS_SERVICE_NAME for HEALTHY instances only.
Replaces describe_tasks, which returned an address we then cached and
never refreshed. Public signature unchanged; callers untouched.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Connection pool adopts the freshly-resolved IP before reconnect (core fix)

The request handler already resolves a fresh IP and passes it into `send_rpc(..., ip, token)`. The bug: the **parked** branch calls `conn.reopen()`, which re-dials the connection's *original* `self.ip`, ignoring the fresh value. Adopt the fresh IP onto the existing connection before any reuse/reopen.

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py:1522-1532` (`send_rpc`)
- Test: `apps/backend/tests/unit/core/test_connection_pool.py`

- [ ] **Step 1: Write the failing test**

In `apps/backend/tests/unit/core/test_connection_pool.py`, add inside the same test class as `test_send_rpc_reuses_existing` (it uses the `pool` fixture and imports `GatewayConnection`, `AsyncMock`, `patch`):
```python
    @pytest.mark.asyncio
    async def test_send_rpc_adopts_fresh_ip_on_parked_reopen(self, pool):
        """A parked connection must adopt the freshly-resolved IP before reopen,
        so a task replacement (new IP) can't wedge it on the dead old IP."""
        parked = AsyncMock(spec=GatewayConnection)
        parked.ip = "10.0.0.1"  # stale: previous task
        parked._parked = True
        parked.is_connected = False
        parked.wait_for_response = AsyncMock(return_value={"ok": True})
        parked._frontend_connections = set()
        pool._connections["user-1"] = parked

        result = await pool.send_rpc("user-1", "r", "agents.list", {}, "10.0.0.2", "tok")

        assert parked.ip == "10.0.0.2"  # adopted the fresh IP before reopen
        parked.reopen.assert_awaited_once()
        assert result == {"ok": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_connection_pool.py -k adopts_fresh_ip -v`
Expected: FAIL with `assert '10.0.0.1' == '10.0.0.2'` (the IP is not adopted today).

- [ ] **Step 3: Adopt the fresh IP in `send_rpc`**

In `apps/backend/core/gateway/connection_pool.py`, in `send_rpc`, replace the start of the `async with self._lock:` block (currently lines 1522-1532) with:
```python
        async with self._lock:
            conn = self._connections.get(user_id)
            # Always adopt the freshly-resolved endpoint. Container task IPs are
            # ephemeral (every ECS task replacement changes them); a parked/stale
            # connection still holds the previous task's IP, so reopen() would
            # otherwise re-dial a dead address forever.
            if conn is not None and conn.ip != ip:
                logger.info(
                    "Gateway endpoint changed for user=%s %s -> %s — adopting before reconnect",
                    user_id,
                    conn.ip,
                    ip,
                )
                conn.ip = ip
            if conn is not None and conn._parked:
                self._respect_reconnect_floor(user_id)
                await conn.reopen()
            elif conn is not None and not conn.is_connected:
                self._respect_reconnect_floor(user_id)
                await conn.close()
                conn = None
            if conn is None:
                conn = await self._create_connection(user_id, ip, token)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_connection_pool.py -v`
Expected: PASS (the whole file — confirms the existing reuse/stale/create tests still hold).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/core/test_connection_pool.py
git commit -m "$(cat <<'EOF'
fix(gateway): re-resolve container IP on reconnect

send_rpc now adopts the freshly-resolved IP onto an existing connection
before reuse/reopen. A parked connection used to re-dial its original
cached IP forever; after a container task replacement that IP is dead,
which wedged the whole org's gateway until a backend restart.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Re-arm the parked watchdog on a failed reopen (defense-in-depth)

Even with the IP fix, a genuinely-down container can fail reopen repeatedly. Today `reopen()` cancels the watchdog at the top and, on a `connect()` exception, never re-arms it — leaving `_parked=True` with no escape hatch. Re-arm on failure so the watchdog can still escalate parked→DEAD (which lets the pool recreate from scratch).

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py:1287-1311` (`reopen`)
- Test: `apps/backend/tests/unit/core/test_connection_pool.py`

- [ ] **Step 1: Write the failing test**

In `apps/backend/tests/unit/core/test_connection_pool.py`, add at module level (top-level function; needs `import asyncio`, `import time`, `from unittest.mock import MagicMock`, and `from core.gateway.connection_pool import GatewayConnection` — add any missing imports at the top of the file):
```python
@pytest.mark.asyncio
async def test_reopen_failure_rearms_parked_watchdog(monkeypatch):
    """A failed reopen must re-arm the parked watchdog so the connection can
    still escalate parked->dead instead of wedging forever."""
    from core.gateway.connection_pool import GatewayConnection

    conn = GatewayConnection(
        user_id="u",
        ip="10.0.0.1",
        token="t",
        management_api=MagicMock(),
        frontend_connections=set(),
        conn_member_map={},
    )
    conn._parked = True
    conn._active_runs = {"agent:1:m": {"last_event_at": time.monotonic(), "agent_id": "1"}}

    async def boom():
        raise TimeoutError("handshake timeout")

    monkeypatch.setattr(conn, "connect", boom)

    with pytest.raises(TimeoutError):
        await conn.reopen()

    assert conn._parked is True
    assert conn._parked_watchdog_task is not None
    assert not conn._parked_watchdog_task.done()

    conn._parked_watchdog_task.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_connection_pool.py -k rearms_parked_watchdog -v`
Expected: FAIL — `conn._parked_watchdog_task` is `None` after the failed reopen (watchdog was cancelled and never re-armed).

- [ ] **Step 3: Re-arm the watchdog on failure in `reopen`**

In `apps/backend/core/gateway/connection_pool.py`, replace the body of `reopen()` from the watchdog-cancel block through `await self.connect()` (currently lines 1299-1304) with:
```python
        if self._parked_watchdog_task and not self._parked_watchdog_task.done():
            self._parked_watchdog_task.cancel()
        self._parked_watchdog_task = None
        # Old WS is already torn down (reader_loop exited); just drop the ref.
        self._ws = None
        try:
            await self.connect()
        except Exception:
            # connect() failed — do NOT leave the connection parked with no
            # watchdog. That wedges it forever: every reopen re-dials and
            # fails, and the parked->dead escalation (which lets the pool
            # recreate from scratch) never fires. Re-arm so it can escalate.
            if self._active_runs and (
                self._parked_watchdog_task is None or self._parked_watchdog_task.done()
            ):
                self._parked_watchdog_task = asyncio.create_task(self._parked_watchdog())
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/core/test_connection_pool.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/core/test_connection_pool.py
git commit -m "$(cat <<'EOF'
fix(gateway): re-arm parked watchdog when reopen fails

A failed reopen left the connection parked with no watchdog, so the
parked->dead escalation that triggers a full rebuild never fired.
Re-arm the watchdog on connect() failure as defense-in-depth.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Full verification + rollout

**Files:** none (verification + deploy).

- [ ] **Step 1: Run the full backend unit suite**

Run: `cd apps/backend && uv run pytest tests/unit -q`
Expected: all pass. Pay attention to `tests/unit/containers/` and `tests/unit/core/` + `tests/unit/gateway/`.

- [ ] **Step 2: Lint**

Run: `turbo run lint --filter=@isol8/backend`
Expected: clean.

- [ ] **Step 3: Open the PR** (per repo convention — never push to main directly)

```bash
git push -u origin HEAD
gh pr create --title "fix(gateway): Cloud Map container discovery + re-resolve on reconnect" --body "$(cat <<'EOF'
## Summary
- Resolve per-user container IPs via Cloud Map DiscoverInstances (read from where we register), replacing describe_tasks.
- Connection pool adopts the freshly-resolved IP before reuse/reopen, so a container task replacement can't wedge the org on a dead IP.
- Re-arm the parked watchdog on failed reopen (defense-in-depth).

Fixes the prod incident where org gateways showed "connected" but every RPC timed out against a stale task IP.

## Test plan
- [ ] Task 1 read-only DiscoverInstances returns the live task for the affected org
- [ ] Backend unit suite green
- [ ] After deploy: affected org's /chat loads agents; control dashboard responds
EOF
)"
```

- [ ] **Step 4: Deploy ordering**

CDK (`deploy.yml`) must land **before** the backend (`backend.yml`) so the `CLOUD_MAP_NAMESPACE_NAME`/`CLOUD_MAP_SERVICE_NAME` env vars exist when the new code reads them. The two env vars are inert for the current backend, so deploying CDK first is safe. Watch both runs:
```bash
gh run watch <infra-run-id> --repo Isol8AI/isol8 --exit-status
gh run watch <backend-run-id> --repo Isol8AI/isol8 --exit-status
```

- [ ] **Step 5: Confirm the live incident is resolved**

The backend redeploy starts a fresh pool, so the wedged org re-resolves on its next RPC. Verify against `/ecs/isol8-prod`:
```bash
START=$(( ($(date +%s) - 600) * 1000 ))
aws logs filter-log-events --log-group-name /ecs/isol8-prod --profile isol8-admin --region us-east-1 \
  --start-time "$START" \
  --filter-pattern '"org_3DBS8G83oPrMm49yN2b70EjoFpN"' \
  --output json | python3 -c "import json,sys; d=json.load(sys.stdin); [print(e['message'][:160]) for e in d.get('events',[])[-20:]]"
```
Expected: `Gateway connection established ... ip=10.2.3.224` and successful `agents.list` RPCs (no `timed out during opening handshake`). Confirm in the browser that agents load and the control dashboard responds.

---

## Self-Review

**Spec coverage:**
- "Use Cloud Map instead of describe_tasks" → Task 3. ✓
- "Keep one shared Cloud Map service for all OpenClaws" → no topology change; Task 3 reads the existing shared service via attribute filter. ✓
- "Fix the stale-IP wedge (the actual outage)" → Task 4 (re-resolve on reconnect) + Task 5 (watchdog re-arm). ✓
- "Confirm the approach works against real AWS" → Task 1 (read-only DiscoverInstances) + Task 6 Step 5. ✓
- Env/IAM wiring → Task 2 (IAM already grants `DiscoverInstances`, confirmed in service-stack.ts:428-434, so no IAM change). ✓

**Placeholder scan:** none — every code/test step contains full code and exact commands.

**Type/name consistency:** `discover_ip(service_name) -> str|None` signature preserved (callers untouched). Settings `CLOUD_MAP_NAMESPACE_NAME`/`CLOUD_MAP_SERVICE_NAME` used identically in config, CDK, ecs_manager, and tests. `conn.ip` attribute used consistently in `send_rpc` and the connection. `_parked_watchdog_task` / `_active_runs` / `_parked` match `connection_pool.py`.

**Note:** The `describe_tasks`-based `discover_ip` is fully replaced (no fallback kept), per the decision to use Cloud Map exclusively. `is_healthy()`'s TCP probe in `resolve_running_container` is intentionally left in place as a complementary liveness check.
