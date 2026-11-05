"""Real Temporal dev server on localhost:7233 (Lane T recipe); every test here is skipped when none answers."""

import socket
import uuid

import pytest
import pytest_asyncio


def _dev_server_up() -> bool:
    try:
        socket.create_connection(("localhost", 7233), timeout=0.5).close()
        return True
    except OSError:
        return False


def pytest_collection_modifyitems(items):
    if _dev_server_up():
        return
    skip = pytest.mark.skip(reason="no Temporal dev server on localhost:7233")
    for item in items:
        if "integration/super_agent/automations" in str(item.fspath):
            item.add_marker(skip)


@pytest_asyncio.fixture
async def client():
    from temporalio.client import Client

    from core.temporal import client as tc

    c = await Client.connect("localhost:7233", namespace="default")
    tc.set_client(c)
    yield c
    tc.set_client(None)


@pytest.fixture
def ws():
    """A fresh workspace id per test, so schedules from other tests never collide."""
    return f"org_it{uuid.uuid4().hex[:10]}"
