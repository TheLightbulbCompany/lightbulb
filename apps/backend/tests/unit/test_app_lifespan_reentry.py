"""The app lifespan is entered once per test by FastAPI's TestClient (the shared ``client``
fixture), so everything it starts must be startable again in the same process. The hosted
MCP server's session manager is once-per-instance; a module-level instance failed every
lifespan entry after the first (2026-10-02, #3547)."""

from fastapi.testclient import TestClient


def test_the_lifespan_can_be_entered_twice_in_one_process(app):
    for _ in range(2):
        with TestClient(app) as client:
            # The MCP door is live inside the lifespan: the SDK answers an unauthenticated
            # call itself (401 + WWW-Authenticate) instead of the 503 the door gives outside.
            assert client.post("/mcp", json={}).status_code == 401


def test_the_mcp_door_answers_503_outside_a_lifespan(app):
    assert TestClient(app).post("/mcp", json={}).status_code == 503


def test_an_unknown_path_is_still_a_404_outside_a_lifespan(app):
    # The door is mounted at "/": it must not turn every unrouted path into a 503.
    assert TestClient(app).get("/api/v1/no-such-route").status_code == 404
