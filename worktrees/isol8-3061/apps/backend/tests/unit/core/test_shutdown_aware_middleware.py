import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
