"""Process-wide Paperclip WebSocket clients projected to private AppSync streams.

main.py builds the one broker during lifespan startup and publishes it with
``set_broker``; ``GET /messaging/live-config`` reads it back with ``get_broker``
to ``ensure_user`` before handing the client its AppSync channel."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any, Awaitable, Callable

from core.observability.metrics import gauge, put_metric
from core.services.live_events import get_live_event_publisher
from core.services.messaging import make_live_event

logger = logging.getLogger(__name__)

_CLIENT_CLOSE_TIMEOUT = 3.0
# Web clients re-fetch live-config every 5 min (useLiveEvents refreshInterval), so
# 10 min tolerates one missed refresh before a user's upstream socket is released.
_IDLE_RELEASE_S = 600.0
_PAPERCLIP_EVENTS = frozenset(
    {
        "teams.activity.logged",
        "teams.agent.status",
        "teams.heartbeat.run.queued",
        "teams.heartbeat.run.status",
        "teams.heartbeat.run.event",
        "teams.heartbeat.run.log",
        "teams.external_object.updated",
        "teams.stream.resumed",
    }
)


class TeamsEventBroker:
    """Keep one upstream Paperclip client per active application user."""

    def __init__(
        self,
        *,
        client_factory: Callable[..., Any],
        resolve_company_id: Callable[[str], Awaitable[str]],
        resolve_session_cookie: Callable[[str], Awaitable[str]],
    ) -> None:
        self._client_factory = client_factory
        self._resolve_company_id = resolve_company_id
        self._resolve_cookie = resolve_session_cookie
        self._clients: dict[tuple[str, str], Any] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._last_seen: dict[tuple[str, str], float] = {}

    async def ensure_user(self, owner_id: str, user_id: str) -> bool:
        """Idempotently attach the user's Paperclip live stream.

        AppSync has no disconnect callback, so each call (every live-config fetch)
        stamps a last-seen time and ``release_idle`` closes clients nobody has
        refreshed within ``_IDLE_RELEASE_S``; the next call re-opens one.
        """
        key = (owner_id, user_id)
        self._last_seen[key] = monotonic()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._clients.get(key)
            if existing is not None and existing.is_alive():
                return True
            if existing is not None:
                try:
                    await existing.close()
                except Exception:
                    logger.warning("teams broker dead-client close failed", exc_info=True)
                self._clients.pop(key, None)
            try:
                company_id, cookie = await asyncio.gather(
                    self._resolve_company_id(owner_id),
                    self._resolve_cookie(user_id),
                )

                async def _on_event(event: dict[str, Any]) -> None:
                    await self._handle_event(owner_id, user_id, event)

                client = self._client_factory(
                    user_id=user_id,
                    company_id=company_id,
                    cookie=cookie,
                    on_event=_on_event,
                )
                await client.start()
            except Exception:
                put_metric("teams.broker.subscribe", dimensions={"outcome": "error"})
                logger.warning("teams broker attach failed for user=%s", user_id, exc_info=True)
                return False
            self._clients[key] = client
            put_metric("teams.broker.subscribe", dimensions={"outcome": "ok"})
            gauge("teams.broker.users.active", len(self._clients))
            return True

    async def release_idle(self) -> None:
        """Close clients whose user has not refreshed live-config recently.

        Called from main.py's 60 s gauge loop. Takes each key's lock so a
        concurrent ``ensure_user`` either refreshes first (kept) or re-opens after.
        """
        cutoff = monotonic() - _IDLE_RELEASE_S
        for key in [k for k, seen in self._last_seen.items() if seen <= cutoff]:
            async with self._locks.setdefault(key, asyncio.Lock()):
                if self._last_seen.get(key, cutoff + 1) > cutoff:
                    continue
                self._last_seen.pop(key, None)
                client = self._clients.pop(key, None)
            if client is not None:
                try:
                    await asyncio.wait_for(client.close(), timeout=_CLIENT_CLOSE_TIMEOUT)
                except Exception:
                    logger.warning("teams broker idle-client close failed", exc_info=True)
                put_metric("teams.broker.released", dimensions={"reason": "idle"})
        gauge("teams.broker.users.active", len(self._clients))

    async def _handle_event(self, owner_id: str, user_id: str, event: dict[str, Any]) -> None:
        event_name = f"teams.{event.get('type', 'unknown')}"
        if event_name not in _PAPERCLIP_EVENTS:
            put_metric("teams.broker.event.received", dimensions={"event_type": "unknown"})
            return
        put_metric("teams.broker.event.received", dimensions={"event_type": event_name})
        live = make_live_event(
            "paperclip.event",
            {"eventName": event_name, "payload": event.get("payload", {})},
        )
        try:
            await get_live_event_publisher().publish_user(owner_id, user_id, live)
        except Exception:
            # No Paperclip payload/body in logs: activity events may contain room text.
            put_metric("teams.broker.fanout", dimensions={"outcome": "error"})
            logger.warning("teams broker AppSync publish failed for user=%s", user_id, exc_info=True)
        else:
            put_metric("teams.broker.fanout", dimensions={"outcome": "ok"})

    async def shutdown(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()

        async def _close(client: Any) -> None:
            try:
                await asyncio.wait_for(client.close(), timeout=_CLIENT_CLOSE_TIMEOUT)
            except Exception:
                logger.warning("teams broker client close failed", exc_info=True)

        if clients:
            await asyncio.gather(*(_close(client) for client in clients))
        gauge("teams.broker.users.active", 0)


_singleton: TeamsEventBroker | None = None


def set_broker(broker: TeamsEventBroker | None) -> None:
    """Called by main.py during lifespan startup/shutdown."""
    global _singleton
    _singleton = broker


def get_broker() -> TeamsEventBroker | None:
    """The live broker, or None before startup has built one."""
    return _singleton
