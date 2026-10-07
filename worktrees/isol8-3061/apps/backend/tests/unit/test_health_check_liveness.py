from fastapi.testclient import TestClient


def test_health_does_not_touch_dynamodb(monkeypatch):
    import main

    called = {"ddb": False}

    def _boom(*a, **k):
        called["ddb"] = True
        raise AssertionError("health must not call DynamoDB")

    monkeypatch.setattr(main, "get_table", _boom, raising=False)
    main._started = True
    client = TestClient(main.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert called["ddb"] is False


def test_health_503_before_startup_complete():
    import main

    main._started = False
    client = TestClient(main.app)
    assert client.get("/health").status_code == 503
    main._started = True  # restore
