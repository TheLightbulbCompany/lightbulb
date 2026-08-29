"""Tests for the Secrets Manager-backed integration credential store.

The real boto3 ``secretsmanager`` client is replaced by an in-memory fake whose
nested ``.exceptions`` namespace exposes the two exception classes the store
catches off the client instance (``ResourceExistsException`` /
``ResourceNotFoundException``).
"""

import json

import pytest

from core.config import settings
from core.services.credentials import integration_secret_store as store


class _ResourceExists(Exception):
    pass


class _ResourceNotFound(Exception):
    pass


class _InvalidRequest(Exception):
    """Stand-in for botocore InvalidRequestException (secret pending deletion)."""


# Sentinel store value marking a secret as SCHEDULED FOR DELETION: it still
# 'exists' but GetSecretValue refuses it, exactly as real Secrets Manager does.
_PENDING_DELETION = object()


class _FakeSM:
    """In-memory Secrets Manager fake keyed by secret Name/SecretId."""

    # Mirror sm.exceptions.<Class> so the store's create/get/delete guards
    # can be exercised without boto3.
    class exceptions:
        ResourceExistsException = _ResourceExists
        ResourceNotFoundException = _ResourceNotFound
        InvalidRequestException = _InvalidRequest

    def __init__(self, store_dict):
        self._store = store_dict

    def create_secret(self, Name, SecretString):
        if Name in self._store:
            raise _ResourceExists(f"{Name} already exists")
        self._store[Name] = SecretString

    def put_secret_value(self, SecretId, SecretString):
        # Real SM raises ResourceNotFound here too, but the store only calls
        # this after a create raised ResourceExists, so the secret is present.
        self._store[SecretId] = SecretString

    def get_secret_value(self, SecretId):
        if SecretId not in self._store:
            raise _ResourceNotFound(f"{SecretId} not found")
        if self._store[SecretId] is _PENDING_DELETION:
            raise _InvalidRequest("You can't perform this operation on the secret because it was marked for deletion.")
        return {"SecretString": self._store[SecretId]}

    def delete_secret(self, SecretId, ForceDeleteWithoutRecovery=False):
        if SecretId not in self._store:
            raise _ResourceNotFound(f"{SecretId} not found")
        del self._store[SecretId]

    def describe_secret(self, SecretId):
        if SecretId not in self._store:
            raise _ResourceNotFound(f"{SecretId} not found")
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:000:secret:{SecretId}"}


@pytest.fixture
def fake_sm(monkeypatch):
    secrets: dict[str, str] = {}
    monkeypatch.setattr(store, "_client", lambda: _FakeSM(secrets))
    return secrets


def test_secret_name_format(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    assert store._secret_name("org_1", "linear") == "lightbulb/prod/integrations/org_1/linear"


def test_secret_name_defaults_to_dev(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "")
    assert store._secret_name("org_1", "linear") == "lightbulb/dev/integrations/org_1/linear"


@pytest.mark.asyncio
async def test_put_creates_secret_and_returns_arn(fake_sm):
    arn = await store.put("org_1", "linear", {"token": "lin_secret"})

    name = store._secret_name("org_1", "linear")
    assert name in fake_sm
    assert arn == f"arn:aws:secretsmanager:us-east-1:000:secret:{name}"


@pytest.mark.asyncio
async def test_put_stores_json_of_credential(fake_sm):
    cred = {"token": "lin_secret", "scope": "read"}
    await store.put("org_1", "linear", cred)

    name = store._secret_name("org_1", "linear")
    # SecretString is the JSON-encoded credential dict and round-trips exactly.
    assert json.loads(fake_sm[name]) == cred


@pytest.mark.asyncio
async def test_second_put_updates_via_put_secret_value(fake_sm):
    # First create, then a second put for the same owner/provider must hit
    # ResourceExistsException and fall through to put_secret_value (update).
    await store.put("org_1", "linear", {"token": "first"})
    await store.put("org_1", "linear", {"token": "second"})

    name = store._secret_name("org_1", "linear")
    assert json.loads(fake_sm[name]) == {"token": "second"}


@pytest.mark.asyncio
async def test_get_returns_parsed_dict(fake_sm):
    await store.put("org_1", "linear", {"token": "lin_secret"})
    got = await store.get("org_1", "linear")
    assert got == {"token": "lin_secret"}


@pytest.mark.asyncio
async def test_get_absent_returns_none(fake_sm):
    assert await store.get("org_1", "missing") is None


@pytest.mark.asyncio
async def test_put_then_get_roundtrip(fake_sm):
    cred = {"access_token": "a", "refresh_token": "r", "nested": {"k": 1}}
    await store.put("org_1", "google", cred)
    assert await store.get("org_1", "google") == cred


@pytest.mark.asyncio
async def test_delete_removes_secret(fake_sm):
    await store.put("org_1", "linear", {"token": "x"})
    await store.delete("org_1", "linear")

    assert store._secret_name("org_1", "linear") not in fake_sm
    assert await store.get("org_1", "linear") is None


@pytest.mark.asyncio
async def test_delete_absent_is_noop(fake_sm):
    # No secret exists -> ResourceNotFoundException is swallowed, no raise.
    await store.delete("org_1", "never_existed")
    assert await store.get("org_1", "never_existed") is None


@pytest.mark.asyncio
async def test_get_pending_deletion_returns_none(fake_sm):
    """A secret SCHEDULED FOR DELETION reads as not-connected (None), never 500.

    GetSecretValue raises InvalidRequestException for a secret in the deletion
    recovery window; get() must treat that like absence so the connections UI
    re-stages a fresh connect instead of 500ing for the whole 7-30 day window.
    """
    name = store._secret_name("org_x", "aws")
    fake_sm[name] = _PENDING_DELETION
    assert await store.get("org_x", "aws") is None
