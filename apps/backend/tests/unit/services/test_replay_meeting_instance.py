from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts.replay_meeting_instance import validate


def invite(**changes):
    return SimpleNamespace(
        **(
            {
                "method": "REQUEST",
                "instance_of": "series",
                "recurrence": None,
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "start": datetime.fromtimestamp(100, timezone.utc),
                "end": datetime.fromtimestamp(200, timezone.utc),
            }
            | changes
        )
    )


def test_exact_unended_instance_is_admitted():
    validate(invite(), parent_uid="series", url="https://meet.google.com/abc-defg-hij", now=150)


@pytest.mark.parametrize(
    "change",
    [
        {"instance_of": "other"},
        {"method": "CANCEL"},
        {"recurrence": {"rule": "daily"}},
        {"meeting_url": "https://meet.google.com/other"},
        {"end": datetime.fromtimestamp(149, timezone.utc)},
    ],
)
def test_replay_refuses_wrong_or_ended_occurrence(change):
    with pytest.raises(ValueError):
        validate(invite(**change), parent_uid="series", url="https://meet.google.com/abc-defg-hij", now=150)


def test_operator_times_must_match():
    with pytest.raises(ValueError, match="operator-authorized"):
        validate(
            invite(),
            parent_uid="series",
            url="https://meet.google.com/abc-defg-hij",
            now=150,
            expected_start=datetime.fromtimestamp(101, timezone.utc),
        )


@pytest.mark.parametrize(
    "verified,authorized,accepted",
    [
        (True, "admin@example.com", True),
        (False, "admin@example.com", False),
        (True, "other@example.com", False),
        (True, None, False),
    ],
)
def test_google_sender_exception_requires_verified_signature_and_exact_authorization(
    monkeypatch, verified, authorized, accepted
):
    from scripts import replay_meeting_instance as replay

    class Verifier:
        headers = [(b"DKIM-Signature", b"d=google.com; s=test;")]

        def __init__(self, *args, **kwargs):
            pass

        def verify(self, *args, **kwargs):
            return verified

    monkeypatch.setattr(replay.dkim, "DKIM", Verifier)
    monkeypatch.setattr(replay.mi, "parse", lambda text: object())
    raw = b"From: admin@example.com\r\nContent-Type: text/calendar\r\n\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    if accepted:
        assert replay.authenticated_mime(raw, authorized)[1] == "admin@example.com"
    else:
        with pytest.raises(ValueError, match="DKIM"):
            replay.authenticated_mime(raw, authorized)
