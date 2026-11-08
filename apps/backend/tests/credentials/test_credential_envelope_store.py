"""Phase 2 task 5: with CREDENTIAL_ENVELOPE_STORE on, integration credentials go to DynamoDB under
a KMS envelope and no new per-owner Secrets Manager secret is created. Off (the default) is
byte-for-byte today's Secrets Manager path."""

import json
import os

import pytest

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

    def __init__(self, secrets):
        self.secrets = secrets

    def create_secret(self, Name, SecretString):
        if Name in self.secrets:
            raise self.exceptions.ResourceExistsException()
        self.secrets[Name] = SecretString

    def put_secret_value(self, SecretId, SecretString):
        self.secrets[SecretId] = SecretString

    def describe_secret(self, SecretId):
        return {"ARN": f"arn:sm:{SecretId}"}

    def get_secret_value(self, SecretId):
        if SecretId not in self.secrets:
            raise _NotFound()
        return {"SecretString": self.secrets[SecretId]}

    def delete_secret(self, SecretId, ForceDeleteWithoutRecovery):
        if self.secrets.pop(SecretId, None) is None:
            raise _NotFound()


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

    def put_item(self, Item):
        self.items[(Item["owner_id"], Item["provider"])] = Item

    def get_item(self, Key, ConsistentRead=False):
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
    secrets, kms, table = {}, _FakeKMS(), _FakeTable()
    monkeypatch.setattr(store, "_client", lambda: _FakeSM(secrets))
    monkeypatch.setattr(store, "_kms", lambda: kms)
    monkeypatch.setattr(store, "_envelope_table", lambda: table)
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "CREDENTIAL_KMS_KEY_ARN", "arn:aws:kms:us-east-1:1:key/k")
    return secrets, kms, table


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "CREDENTIAL_ENVELOPE_STORE", True)


def test_flag_defaults_off():
    assert settings.CREDENTIAL_ENVELOPE_STORE is False


@pytest.mark.asyncio
async def test_off_writes_secrets_manager_only(stores):
    secrets, kms, table = stores
    await store.put("org_a", "github", {"token": "t"})
    assert list(secrets) == ["lightbulb/dev/integrations/org_a/github"]
    assert table.items == {} and kms.calls == []


@pytest.mark.asyncio
async def test_on_creates_no_secret_and_stores_no_plaintext(stores, flag_on):
    secrets, kms, table = stores
    await store.put("org_a", "website-login/vault_1", {"password": "hunter2"})
    assert secrets == {}
    item = table.items[("org_a", "website-login/vault_1")]
    assert "hunter2" not in json.dumps({k: str(v) for k, v in item.items()})
    # The data key is bound to the tenant and the provider.
    assert kms.calls[0][2] == {"owner_id": "org_a", "provider": "website-login/vault_1"}
    assert await store.get("org_a", "website-login/vault_1") == {"password": "hunter2"}


@pytest.mark.asyncio
async def test_on_without_key_falls_back_to_secrets_manager(stores, flag_on, monkeypatch):
    secrets, _, table = stores
    monkeypatch.setattr(settings, "CREDENTIAL_KMS_KEY_ARN", "")
    await store.put("org_a", "github", {"token": "t"})
    assert "lightbulb/dev/integrations/org_a/github" in secrets and table.items == {}


@pytest.mark.asyncio
async def test_envelope_cannot_be_read_as_another_owner(stores, flag_on):
    _, _, table = stores
    await store.put("org_a", "github", {"token": "t"})
    table.items[("org_b", "github")] = {**table.items[("org_a", "github")], "owner_id": "org_b"}
    with pytest.raises(ValueError):
        await store.get("org_b", "github")


@pytest.mark.asyncio
async def test_get_prefers_envelope_then_legacy_secret(stores, flag_on):
    secrets, _, _ = stores
    secrets["lightbulb/dev/integrations/org_a/github"] = json.dumps({"token": "old"})
    assert await store.get("org_a", "github") == {"token": "old"}  # legacy still readable
    await store.put("org_a", "github", {"token": "new"})
    assert await store.get("org_a", "github") == {"token": "new"}


@pytest.mark.asyncio
async def test_delete_removes_both_copies(stores, flag_on):
    secrets, _, table = stores
    secrets["lightbulb/dev/integrations/org_a/github"] = json.dumps({"token": "old"})
    await store.put("org_a", "github", {"token": "new"})
    assert await store.delete("org_a", "github") is True
    assert secrets == {} and table.items == {}
    assert await store.get("org_a", "github") is None
    assert await store.delete("org_a", "github") is False


@pytest.mark.asyncio
async def test_owner_wipe_sweeps_envelopes(stores, flag_on, monkeypatch):
    _, _, table = stores
    await store.put("org_a", "github", {"token": "a"})
    await store.put("org_b", "github", {"token": "b"})
    monkeypatch.setattr(owner_secret_sweep, "_list_names_with_prefix", lambda sm, prefix: [])
    monkeypatch.setattr(owner_secret_sweep.boto3, "client", lambda *a, **k: _FakeSM({}))
    assert await owner_secret_sweep.list_owner_secret_names("org_a") == ["credential-envelopes/org_a/github"]
    assert await owner_secret_sweep.delete_owner_secrets("org_a") == ["credential-envelopes/org_a/github"]
    assert list(table.items) == [("org_b", "github")]
