"""Process-wide singleton that brokers Paperclip live events to subscribed
browser WS connections.

Owns one ``PaperclipEventClient`` per *user* (not per browser tab). When
the last subscriber for a user disconnects, schedules a ``grace_seconds``
delayed teardown so reconnect storms (e.g. mobile network blip) don't
churn the upstream WS.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol

from core.observability.metrics import gauge, put_metric, timing

logger = logging.getLogger(__name__)


_DEFAULT_GRACE_SECONDS = 30.0


class _Client(Protocol):
    """The narrow surface of PaperclipEventClient the broker depends on."""

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    def is_alive(self) -> bool: ...


ClientFactory = Callable[..., _Client]
"""Signature: (user_id, company_id, cookie, on_event) -> client.

Kept as a callable so tests can swap in a fake without monkeypatching."""


class TeamsEventBroker:
    """Routes Paperclip events to subscribed browser conn-IDs."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory,
        management_api: Any,
        connection_service: Any,
        resolve_company_id: Callable[[str], Awaitable[str]],
        resolve_session_cookie: Callable[[str], Awaitable[str]],
        grace_seconds: float = _DEFAULT_GRACE_SECONDS,
    ) -> None:
        self._client_factory = client_factory
        self._mgmt = management_api
        self._conn_svc = connection_service
        self._resolve_company_id = resolve_company_id
        self._resolve_cookie = resolve_session_cookie
        self._grace_seconds = grace_seconds

        # State:
        self._clients: dict[str, _Client] = {}
        self._subscribers: dict[str, set[str]] = {}
        self._grace_tasks: dict[str, asyncio.Task[None]] = {}
        # Per-user lock prevents two near-simultaneous subscribes from
        # opening duplicate backing clients.
        self._locks: dict[str, asyncio.Lock] = {}

    def _user_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def subscribe(self, user_id: str, connection_id: str) -> None:
        """Register ``connection_id`` as a subscriber for ``user_id``.

        Idempotent — calling twice with the same conn-ID is a no-op.
        Cancels any pending teardown grace task.
        """
        async with self._user_lock(user_id):
            self._subscribers.setdefault(user_id, set()).add(connection_id)

            # Cancel pending grace teardown if one is scheduled.
            grace = self._grace_tasks.pop(user_id, None)
            if grace and not grace.done():
                grace.cancel()

            existing = self._clients.get(user_id)
            if existing is not None:
                # A backing client that exhausted its reconnect attempts
                # (~15 min of failures) lingers in self._clients but its
                # background loop has terminated — reusing it means the user
                # gets no live updates until all their tabs close long
                # enough for grace teardown. Replace dead clients before
                # reusing.
                if existing.is_alive():
                    put_metric("teams.broker.subscribe", dimensions={"outcome": "reused"})
                    return
                logger.warning(
                    "teams broker: replacing dead backing client user=%s",
                    user_id,
                )
                try:
                    await existing.close()
                except Exception:
                    logger.exception(
                        "teams broker: close() failed on dead client user=%s",
                        user_id,
                    )
                self._clients.pop(user_id, None)
                put_metric("teams.broker.client", dimensions={"event": "replaced_dead"})

            try:
                company_id = await self._resolve_company_id(user_id)
                cookie = await self._resolve_cookie(user_id)
            except Exception:
                logger.exception("teams broker: cannot subscribe user=%s", user_id)
                put_metric(
                    "teams.broker.subscribe",
                    dimensions={"outcome": "factory_failed"},
                )
                subs = self._subscribers.get(user_id)
                if subs is not None:
                    subs.discard(connection_id)
                    if not subs:
                        self._subscribers.pop(user_id, None)
                return

            async def _on_event(event: dict[str, Any]) -> None:
                await self._handle_event(user_id, event)

            client = self._client_factory(
                user_id=user_id,
                company_id=company_id,
                cookie=cookie,
                on_event=_on_event,
            )
            await client.start()
            put_metric("teams.broker.subscribe", dimensions={"outcome": "ok"})
            put_metric("teams.broker.client", dimensions={"event": "opened"})
            self._clients[user_id] = client
            gauge("teams.broker.users.active", len(self._clients))
            logger.info("teams broker: opened backing WS for user=%s company=%s", user_id, company_id)

    async def unsubscribe(self, user_id: str, connection_id: str) -> None:
        """Remove ``connection_id``; if subscriber set is empty, schedule
        teardown after ``grace_seconds``.
        """
        async with self._user_lock(user_id):
            subs = self._subscribers.get(user_id)
            if subs:
                subs.discard(connection_id)
            if subs:
                put_metric(
                    "teams.broker.unsubscribe",
                    dimensions={"outcome": "still_active"},
                )
                return  # still has subscribers
            self._subscribers.pop(user_id, None)

            if user_id in self._clients and user_id not in self._grace_tasks:
                self._grace_tasks[user_id] = asyncio.create_task(
                    self._grace_teardown(user_id),
                )
                put_metric(
                    "teams.broker.unsubscribe",
                    dimensions={"outcome": "grace_started"},
                )

    async def _grace_teardown(self, user_id: str) -> None:
        try:
            await asyncio.sleep(self._grace_seconds)
        except asyncio.CancelledError:
            return
        async with self._user_lock(user_id):
            self._grace_tasks.pop(user_id, None)
            if self._subscribers.get(user_id):
                return  # someone resubscribed during the sleep race
            client = self._clients.pop(user_id, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.exception("teams broker: close() failed for user=%s", user_id)
            logger.info("teams broker: closed backing WS for idle user=%s", user_id)
            put_metric("teams.broker.client", dimensions={"event": "closed"})
            gauge("teams.broker.users.active", len(self._clients))
        # We deliberately do NOT pop ``self._locks[user_id]`` here: pruning
        # after releasing the lock creates a race where a subscribe arriving
        # in the gap between ``async with`` exit and the pop holds the OLD
        # lock while a subsequent subscribe lazily creates a FRESH lock,
        # allowing duplicate client creation. The leak from keeping
        # per-former-user locks is bounded by total user count and benign at
        # any realistic scale; correctness > the small memory cost.

    async def _handle_event(self, user_id: str, event: dict[str, Any]) -> None:
        """Wrap and fan out one event to all of the user's connections."""
        put_metric(
            "teams.broker.event.received",
            dimensions={"event_type": event.get("type", "unknown")},
        )
        wrapped = {
            "type": "event",
            "event": f"teams.{event.get('type', 'unknown')}",
            "payload": event.get("payload", {}),
        }
        # Forward upstream metadata if present.
        if "id" in event:
            wrapped["id"] = event["id"]
        if "createdAt" in event:
            wrapped["createdAt"] = event["createdAt"]

        logger.debug(
            "teams broker: dispatching event user=%s event_type=%s",
            user_id,
            event.get("type", "unknown"),
        )

        # Fan out only to browsers that explicitly subscribed via
        # teams.subscribe (tracked in self._subscribers), NOT to every WS
        # connection the user has. ws-connections-by-userId would also
        # return desktop node-role sockets that never asked for Teams
        # events; sending events there is wasted bandwidth + adds
        # parser/backpressure load on the node connection path.
        # Snapshot the subscriber set so the iteration is stable even if
        # an unsubscribe lands mid-loop.
        conn_ids = list(self._subscribers.get(user_id, ()))

        gone_conn_ids: list[str] = []
        with timing("teams.broker.fanout.latency"):
            for conn_id in conn_ids:
                try:
                    ok = self._mgmt.send_message(conn_id, wrapped)
                    if ok is False:
                        put_metric(
                            "teams.broker.fanout",
                            dimensions={"outcome": "gone"},
                        )
                        self._conn_svc.delete_connection(conn_id)
                        gone_conn_ids.append(conn_id)
                    else:
                        put_metric(
                            "teams.broker.fanout",
                            dimensions={"outcome": "ok"},
                        )
                except Exception:
                    logger.exception(
                        "teams broker: send_message failed user=%s conn=%s",
                        user_id,
                        conn_id,
                    )
                    put_metric(
                        "teams.broker.fanout",
                        dimensions={"outcome": "error"},
                    )

        # Stale conns must be removed from the local subscriber set too,
        # not just the DDB row. _grace_teardown checks
        # self._subscribers[user_id] to decide whether to close the backing
        # client; if a stale conn-id lingers there, a single missed
        # teams.unsubscribe pins the upstream WS open forever.
        # Reuses unsubscribe() so cleanup goes through the same lock-guarded
        # path that schedules the grace teardown when subscribers empty.
        for conn_id in gone_conn_ids:
            await self.unsubscribe(user_id, conn_id)

    async def shutdown(self) -> None:
        """Close every backing client + cancel grace tasks. Lifespan shutdown."""
        grace_tasks = list(self._grace_tasks.values())
        for task in grace_tasks:
            task.cancel()
        self._grace_tasks.clear()
        if grace_tasks:
            await asyncio.gather(*grace_tasks, return_exceptions=True)
        clients = list(self._clients.values())
        self._clients.clear()
        self._subscribers.clear()
        for client in clients:
            try:
                await client.close()
            except Exception:
                logger.exception("teams broker shutdown: close() failed")
