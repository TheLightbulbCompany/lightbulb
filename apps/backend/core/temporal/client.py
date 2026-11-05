"""One Temporal client per process (contracts-v2 §16). The FastAPI lifespan and the worker entrypoint
connect it; callers take it from `get_client()`, which raises instead of hanging when it is not up."""

import asyncio
import logging

from temporalio.client import Client

from core.config import settings


logger = logging.getLogger(__name__)


class TemporalUnavailable(RuntimeError):
    """No connected Temporal client in this process (env master off, or the startup connect failed)."""


_client: Client | None = None


async def connect() -> Client:
    kwargs: dict = {"namespace": settings.TEMPORAL_NAMESPACE}
    if settings.TEMPORAL_API_KEY:
        kwargs |= {"api_key": settings.TEMPORAL_API_KEY, "tls": True}
    return await Client.connect(settings.TEMPORAL_ADDRESS, **kwargs)


def set_client(client: Client | None) -> None:
    global _client
    _client = client


def get_client() -> Client:
    if _client is None:
        raise TemporalUnavailable("Temporal client is not connected")
    return _client


async def connect_and_set(timeout_s: float = 10) -> None:
    """Startup's background connect: bounded, and a failure only logs (get_client() keeps raising)."""
    try:
        async with asyncio.timeout(timeout_s):
            set_client(await connect())
    except Exception:
        logger.warning("temporal connect failed", exc_info=True)
