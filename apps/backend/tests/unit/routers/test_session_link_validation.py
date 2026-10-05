"""A rejected session-link body never echoes its token or verifier, and carries a message."""

import pytest

from core.auth import get_human_session

TOKEN = "t" * 200  # too long: refused by validation
VERIFIER = "v" * 20  # too short


@pytest.mark.parametrize(
    "url, body, secret",
    [
        ("/api/v1/public/session-links/open", {"token": TOKEN}, TOKEN),
        ("/api/v1/public/session-copies/redeem", {"code": "c" * 43, "verifier": VERIFIER}, VERIFIER),
        ("/api/v1/session-links/copies", {"token": TOKEN, "challenge": "x"}, TOKEN),
    ],
)
def test_a_rejected_body_is_not_echoed(app, client, mock_current_user, url, body, secret):
    app.dependency_overrides[get_human_session] = mock_current_user
    r = client.post(url, json=body)
    assert r.status_code == 400, r.text
    assert secret not in r.text and r.json()["message"]
