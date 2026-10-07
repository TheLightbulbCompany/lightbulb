"""Tests for core.secrets_manager.create_or_update_secret — create-or-update
that retries past the async ForceDelete name-reuse window."""

import pytest

from core import secrets_manager
from core.secrets_manager import create_or_update_secret


class _ResourceExists(Exception):
    pass


class _InvalidRequest(Exception):
    pass


class _FakeSM:
    class exceptions:
        ResourceExistsException = _ResourceExists
        InvalidRequestException = _InvalidRequest

    def __init__(self):
        self.store: dict[str, str] = {}

    def create_secret(self, Name, SecretString):
        if Name in self.store:
            raise _ResourceExists()
        self.store[Name] = SecretString

    def put_secret_value(self, SecretId, SecretString):
        self.store[SecretId] = SecretString


def test_create_when_absent():
    sm = _FakeSM()
    create_or_update_secret(sm, "n", "v")
    assert sm.store["n"] == "v"


def test_update_when_exists():
    sm = _FakeSM()
    sm.store["n"] = "old"
    create_or_update_secret(sm, "n", "new")
    assert sm.store["n"] == "new"


def test_retries_past_pending_deletion_then_succeeds(monkeypatch):
    monkeypatch.setattr(secrets_manager.time, "sleep", lambda *_: None)
    sm = _FakeSM()
    real_create = sm.create_secret
    state = {"fails": 2}

    def flaky_create(Name, SecretString):
        # First 2 attempts: name still held by a pending deletion. 3rd: freed.
        if state["fails"] > 0:
            state["fails"] -= 1
            raise _InvalidRequest("a secret with this name is already scheduled for deletion")
        real_create(Name=Name, SecretString=SecretString)

    sm.create_secret = flaky_create
    create_or_update_secret(sm, "n", "v")
    assert sm.store["n"] == "v"


def test_non_deletion_invalid_request_raises_immediately(monkeypatch):
    sleeps = []
    monkeypatch.setattr(secrets_manager.time, "sleep", lambda s: sleeps.append(s))
    sm = _FakeSM()

    def bad_create(Name, SecretString):
        raise _InvalidRequest("some other invalid request")

    sm.create_secret = bad_create
    with pytest.raises(_InvalidRequest):
        create_or_update_secret(sm, "n", "v")
    assert sleeps == []  # no retry/backoff for a non-deletion error


def test_persistent_deletion_eventually_raises(monkeypatch):
    monkeypatch.setattr(secrets_manager.time, "sleep", lambda *_: None)
    sm = _FakeSM()

    def always_deleting(Name, SecretString):
        raise _InvalidRequest("scheduled for deletion")

    sm.create_secret = always_deleting
    with pytest.raises(_InvalidRequest):
        create_or_update_secret(sm, "n", "v")


def test_create_only_keeps_the_first_writer_and_returns_its_value():
    sm = _FakeSM()
    sm.get_secret_value = lambda SecretId: {"SecretString": sm.store[SecretId]}
    assert create_or_update_secret(sm, "n", "first", create_only=True) == "first"
    assert create_or_update_secret(sm, "n", "second", create_only=True) == "first"
    assert sm.store["n"] == "first"
