import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.observability.e2e_correlation import E2ECorrelationMiddleware


@pytest.fixture
def app():
    app = FastAPI()
    app.add_middleware(E2ECorrelationMiddleware)

    @app.get("/")
    def root():
        logging.getLogger("test").info("hit")
        return {"ok": True}

    return app


def test_request_without_header_does_not_bind_run_id(app, caplog):
    client = TestClient(app)
    with caplog.at_level(logging.INFO):
        res = client.get("/")
    assert res.status_code == 200
    rec = next((r for r in caplog.records if r.name == "test"), None)
    assert rec is not None
    assert getattr(rec, "e2e_run_id", None) is None


def test_request_with_header_binds_run_id_to_log_context(app, caplog):
    client = TestClient(app)
    with caplog.at_level(logging.INFO):
        res = client.get("/", headers={"X-E2E-Run-Id": "1776572400000-a3b9"})
    assert res.status_code == 200
    rec = next((r for r in caplog.records if r.name == "test"), None)
    assert rec is not None
    assert rec.e2e_run_id == "1776572400000-a3b9"


def test_run_id_is_per_request_not_leaked(app, caplog):
    """Two consecutive requests must not see each other's run_id."""
    client = TestClient(app)
    with caplog.at_level(logging.INFO):
        client.get("/", headers={"X-E2E-Run-Id": "first"})
        client.get("/")  # no header
    recs = [r for r in caplog.records if r.name == "test"]
    assert len(recs) == 2
    assert recs[0].e2e_run_id == "first"
    assert getattr(recs[1], "e2e_run_id", None) is None
