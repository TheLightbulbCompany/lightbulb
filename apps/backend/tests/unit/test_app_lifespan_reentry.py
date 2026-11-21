"""The app lifespan is entered once per test by FastAPI's TestClient (the shared ``client``
fixture), so everything it starts must be startable again in the same process."""

from fastapi.testclient import TestClient


def test_the_lifespan_can_be_entered_twice_in_one_process(app):
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200


def test_an_unknown_path_is_a_404(app):
    assert TestClient(app).get("/api/v1/no-such-route").status_code == 404
