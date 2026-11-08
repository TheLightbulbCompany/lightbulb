"""Phase 2 task 5: with the stage KMS key set (every deployed stage), integration credentials go to
DynamoDB under a KMS envelope and no new per-owner Secrets Manager secret is created. Legacy secrets
stay readable until the next write retires them."""

import json
import os

import pytest

from core import secrets_manager
from core.config import settings
from core.services import owner_secret_sweep
from core.services.credentials import integration_secret_store as store


class _NotFound(Exception):
    pass


class _FakeSM:
    class exceptions:
        ResourceExistsException = type("ResourceExists", (Exception,), {})
        ResourceNotFoundException = _NotFound
        InvalidRequestException = type("InvalidRequest", (Exception,), {})

    def __init__(self, secrets, deletes=None, scheduled=None):
        self.secrets = secrets
        self.deletes = [] if deletes is None else deletes
        # Secrets in their recovery window: unreadable and unwritable until restored, like AWS.
        self.scheduled = set() if scheduled is None else scheduled

    def _writable(self, name):
        if name in self.scheduled:
            raise self.exceptions.InvalidRequestException("secret is scheduled for deletion")

    def create_secret(self, Name, SecretString):
        self._writable(Name)
        if Name in self.secrets:
            raise self.exceptions.ResourceExistsException()
        self.secrets[Name] = SecretString

    def put_secret_value(self, SecretId, SecretString):
        self._writable(SecretId)
        self.secrets[SecretId] = SecretString

    def restore_secret(self, SecretId):
        self.scheduled.discard(SecretId)

    def describe_secret(self, SecretId):
        return {"ARN": f"arn:sm:{SecretId}"}

    def get_secret_value(self, SecretId):
        self._writable(SecretId)
        if SecretId not in self.secrets:
            raise _NotFound()
        return {"SecretString": self.secrets[SecretId]}

    def list_secrets(self, Filters, IncludePlannedDeletion=False, **kwargs):
        prefix = Filters[0]["Values"][0]
        names = [n for n in self.secrets if n.startswith(prefix)]
        return {"SecretList": [{"Name": n} for n in names if IncludePlannedDeletion or n not in self.scheduled]}

    def delete_secret(self, SecretId, ForceDeleteWithoutRecovery=False):
        self.deletes.append((SecretId, ForceDeleteWithoutRecovery))
        if SecretId not in self.secrets:
            raise _NotFound()
        # DeleteSecret lists "The secret is scheduled for deletion" as an InvalidRequestException cause.
        self._writable(SecretId)
        if ForceDeleteWithoutRecovery:
            self.secrets.pop(SecretId)
            self.scheduled.discard(SecretId)
        else:
            self.scheduled.add(SecretId)


class _FakeKMS:
    """Wraps a data key by prefixing it with the context, so a decrypt under another
    owner's context fails exactly like KMS's InvalidCiphertextException would."""

    def __init__(self):
        self.calls = []

    def generate_data_key(self, KeyId, KeySpec, EncryptionContext):
        self.calls.append(("generate", KeyId, EncryptionContext))
        key = os.urandom(32)
        return {"Plaintext": key, "CiphertextBlob": json.dumps(EncryptionContext, sort_keys=True).encode() + b"|" + key}

    def decrypt(self, CiphertextBlob, EncryptionContext):
        ctx, key = CiphertextBlob.split(b"|", 1)
        if ctx != json.dumps(EncryptionContext, sort_keys=True).encode():
            raise ValueError("InvalidCiphertextException")
        return {"Plaintext": key}


class _FakeTable:
    def __init__(self):
        self.items = {}
        self.consistent_reads = []

    def put_item(self, Item):
        self.items[(Item["owner_id"], Item["provider"])] = Item

    def get_item(self, Key, ConsistentRead=False):
        self.consistent_reads.append(ConsistentRead)
        item = self.items.get((Key["owner_id"], Key["provider"]))
        return {"Item": item} if item else {}

    def delete_item(self, Key, ReturnValues="NONE"):
        old = self.items.pop((Key["owner_id"], Key["provider"]), None)
        return {"Attributes": old} if old else {}

    def query(self, KeyConditionExpression, **kwargs):
        owner = KeyConditionExpression.get_expression()["values"][1]
        return {"Items": [i for (o, _), i in self.items.items() if o == owner]}


@pytest.fixture
def stores(monkeypatch):
    secrets, kms, table, deletes, scheduled = {}, _FakeKMS(), _FakeTable(), [], set()
    monkeypatch.setattr(store, "_client", lambda: _FakeSM(secrets, deletes, scheduled))
    monkeypatch.setattr(secrets_manager, "_RETRY_BACKOFFS", ())
    table.deletes, table.scheduled = deletes, scheduled
    monkeypatch.setattr(store, "_kms", lambda: kms)
    monkeypatch.setattr(store, "_envelope_table", lambda: table)
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "CREDENTIAL_KMS_KEY_ARN", "arn:aws:kms:us-east-1:1:key/k")
    return secrets, kms, table


LEGACY = "lightbulb/dev/integrations/org_a/github"


@pytest.mark.asyncio
async def test_put_get_round_trip_creates_no_secret_and_stores_no_plaintext(stores):
    secrets, kms, table = stores
    await store.put("org_a", "website-login/vault_1", {"password": "hunter2"})
    assert secrets == {}
    item = table.items[("org_a", "website-login/vault_1")]
    assert "hunter2" not in json.dumps({k: str(v) for k, v in item.items()})
    # The data key is bound to the tenant and the provider.
    assert kms.calls[0][2] == {"owner_id": "org_a", "provider": "website-login/vault_1"}
    assert await store.get("org_a", "website-login/vault_1") == {"password": "hunter2"}


@pytest.mark.asyncio
async def test_without_key_writes_secrets_manager(stores, monkeypatch):
    secrets, _, table = stores
    monkeypatch.setattr(settings, "CREDENTIAL_KMS_KEY_ARN", "")
    await store.put("org_a", "github", {"token": "t"})
    assert LEGACY in secrets and table.items == {}


@pytest.mark.asyncio
async def test_legacy_only_credential_is_still_readable(stores):
    secrets, _, _ = stores
    secrets[LEGACY] = json.dumps({"token": "old"})
    assert await store.get("org_a", "github") == {"token": "old"}


@pytest.mark.asyncio
async def test_put_retires_the_legacy_secret(stores):
    secrets, _, table = stores
    secrets[LEGACY] = json.dumps({"token": "old"})
    await store.put("org_a", "github", {"token": "new"})
    assert table.deletes == [(LEGACY, False)]  # default recovery window, never forced
    assert LEGACY in table.scheduled
    assert await store.get("org_a", "github") == {"token": "new"}
    await store.put("org_a", "github", {"token": "newer"})  # already scheduled: still fine
    assert await store.get("org_a", "github") == {"token": "newer"}


@pytest.mark.asyncio
async def test_copied_row_fails_to_decrypt(stores):
    _, _, table = stores
    await store.put("org_a", "github", {"token": "t"})
    table.items[("org_b", "github")] = {**table.items[("org_a", "github")], "owner_id": "org_b"}
    with pytest.raises(ValueError):
        await store.get("org_b", "github")


@pytest.mark.asyncio
async def test_envelope_read_is_consistent(stores):
    _, _, table = stores
    await store.put("org_a", "github", {"token": "t"})
    await store.get("org_a", "github")
    assert table.consistent_reads == [True]


@pytest.mark.asyncio
async def test_delete_removes_both_copies_even_when_legacy_is_scheduled(stores):
    secrets, _, table = stores
    secrets[LEGACY] = json.dumps({"token": "old"})
    await store.put("org_a", "github", {"token": "new"})  # schedules the legacy secret
    assert await store.delete("org_a", "github") is True
    assert secrets == {} and table.items == {} and table.scheduled == set()
    assert await store.get("org_a", "github") is None
    assert await store.delete("org_a", "github") is False


@pytest.mark.asyncio
async def test_owner_wipe_sweeps_envelopes_and_scheduled_legacy_secrets(stores, monkeypatch):
    secrets, _, table = stores
    secrets[LEGACY] = json.dumps({"token": "old"})
    await store.put("org_a", "github", {"token": "a"})  # legacy now scheduled for deletion
    await store.put("org_b", "github", {"token": "b"})
    sm = _FakeSM(secrets, table.deletes, table.scheduled)
    monkeypatch.setattr(owner_secret_sweep.boto3, "client", lambda *a, **k: sm)
    expected = [LEGACY, "credential-envelopes/org_a/github"]
    assert await owner_secret_sweep.list_owner_secret_names("org_a") == expected
    assert await owner_secret_sweep.delete_owner_secrets("org_a") == expected
    assert secrets == {} and table.scheduled == set()  # force-deleted, not left recoverable
    assert list(table.items) == [("org_b", "github")]
