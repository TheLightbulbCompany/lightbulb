"""Process-wide singleton accessor for the TeamsEventBroker.

Built once during FastAPI lifespan startup; consumed by
``routers/messaging.py``'s ``GET /messaging/live-config``, which asks the
broker to ``ensure_user`` before handing the client its AppSync channel.
(The WS ``teams.subscribe`` / ``teams.unsubscribe`` dispatch this was
written for went away with the AppSync cutover.) Lives in its own module
so the router can import it without pulling in main.py (circular).
"""

from __future__ import annotations

from typing import Optional

from core.services.teams_event_broker import TeamsEventBroker

_singleton: Optional[TeamsEventBroker] = None


def set_broker(broker: TeamsEventBroker | None) -> None:
    """Called by main.py during startup/shutdown."""
    global _singleton
    _singleton = broker


def get_broker() -> TeamsEventBroker | None:
    """Return the live broker, or None if startup hasn't happened yet
    (e.g. some unit-test bootstrapping)."""
    return _singleton
