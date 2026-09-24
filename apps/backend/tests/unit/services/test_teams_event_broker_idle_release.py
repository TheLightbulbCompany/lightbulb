import pytest

from core.services import teams_event_broker as teb


class _Client:
    def __init__(self, **_):
        self.alive = False
        self.closed = False

    async def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive and not self.closed

    async def close(self):
        self.closed = True


class _Clock:
    now = 1000.0

    def __call__(self):
        return self.now


async def _resolve(_user_id):
    return "x"


@pytest.fixture
def setup(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(teb, "monotonic", clock)  # the module only, not asyncio's clock
    made = []

    def factory(**kw):
        made.append(_Client(**kw))
        return made[-1]

    broker = teb.TeamsEventBroker(client_factory=factory, resolve_company_id=_resolve, resolve_session_cookie=_resolve)
    return broker, clock, made


@pytest.mark.asyncio
async def test_idle_client_released_after_window(setup):
    broker, clock, made = setup
    assert await broker.ensure_user("org_1", "user_1")
    clock.now += teb._IDLE_RELEASE_S - 1
    await broker.release_idle()
    assert not made[0].closed
    clock.now += 2
    await broker.release_idle()
    assert made[0].closed
    assert broker._clients == {}


@pytest.mark.asyncio
async def test_refresh_keeps_client_open(setup):
    broker, clock, made = setup
    await broker.ensure_user("org_1", "user_1")
    for _ in range(3):
        clock.now += teb._IDLE_RELEASE_S - 60
        await broker.ensure_user("org_1", "user_1")
        await broker.release_idle()
    assert len(made) == 1
    assert not made[0].closed


@pytest.mark.asyncio
async def test_registration_after_release_reopens(setup):
    broker, clock, made = setup
    await broker.ensure_user("org_1", "user_1")
    clock.now += teb._IDLE_RELEASE_S + 1
    await broker.release_idle()
    assert await broker.ensure_user("org_1", "user_1")
    assert len(made) == 2
    assert made[0].closed and made[1].is_alive()
    assert broker._clients[("org_1", "user_1")] is made[1]
