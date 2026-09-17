"""Review fix (cheap c): _post_backend retries three times on 5xx/network errors only
(spec §5: "callbacks retry three times on 5xx only and never throw"), never on 4xx."""
import io
import urllib.error

from bridge import turn


def test_a_5xx_retries_up_to_three_times_then_gives_up(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))

    monkeypatch.setattr(turn.urllib.request, "urlopen", fake_urlopen)
    assert turn._post_backend("runs/x/events", {"a": 1}) == 503
    assert len(calls) == turn.POST_BACKEND_ATTEMPTS == 3


def test_a_4xx_is_never_retried(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b""))

    monkeypatch.setattr(turn.urllib.request, "urlopen", fake_urlopen)
    assert turn._post_backend("runs/x/events", {"a": 1}) == 400
    assert len(calls) == 1


def test_a_persistent_network_error_never_throws_and_returns_zero(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(turn.urllib.request, "urlopen", fake_urlopen)
    assert turn._post_backend("runs/x/events", {"a": 1}) == 0  # never raises


def test_a_success_on_the_second_attempt_is_returned(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 502, "bad gateway", {}, io.BytesIO(b""))

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        return FakeResponse()

    monkeypatch.setattr(turn.urllib.request, "urlopen", fake_urlopen)
    assert turn._post_backend("runs/x/events", {"a": 1}) == 200
    assert len(calls) == 2
