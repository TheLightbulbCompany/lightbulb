"""Phase 2 task 5: with the stage KMS key set (every deployed stage), integration credentials go to
DynamoDB under a KMS envelope and no new per-owner Secrets Manager secret is created. Legacy secrets
stay readable; a sealed put leaves them in place (reads prefer the table), and the migration script
removes them later."""

import json
import os

import pytest
from botocore.exceptions import ClientError

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
        listed = [n for n in names if IncludePlannedDeletion or n not in self.scheduled]
        return {"SecretList": [{"Name": n, **({"DeletedDate": 1} if n in self.scheduled else {})} for n in listed]}

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
            raise ClientError({"Error": {"Code": "InvalidCiphertextException"}}, "Decrypt")
        return {"Plaintext": key}


class _FakeTable:
    def __init__(self):
        self.items = {}
        self.consistent_reads = []

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        old = self.items.get((Item["owner_id"], Item["provider"]))
        if (ConditionExpression == "attribute_not_exists(owner_id)" and old) or (
            ConditionExpression == "ciphertext = :read"
            and (old or {}).get("ciphertext") != ExpressionAttributeValues[":read"]
        ):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[(Item["owner_id"], Item["provider"])] = Item

    def get_item(self, Key, ConsistentRead=False):
        self.consistent_reads.append(ConsistentRead)
        item = self.items.get((Key["owner_id"], Key["provider"]))
        return {"Item": item} if item else {}

    def delete_item(self, Key, ReturnValues="NONE"):
        old = self.items.pop((Key["owner_id"], Key["provider"]), None)
        return {"Attributes": old} if old else {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}

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
async def test_put_leaves_the_legacy_secret_and_reads_prefer_the_envelope(stores):
    # A revert to the pre-envelope code must still find a live legacy secret (gate C3).
    secrets, _, table = stores
    secrets[LEGACY] = json.dumps({"token": "old"})
    await store.put("org_a", "github", {"token": "new"})
    assert table.deletes == [] and table.scheduled == set()
    assert secrets[LEGACY] == json.dumps({"token": "old"})
    assert await store.get("org_a", "github") == {"token": "new"}


@pytest.mark.asyncio
async def test_missing_envelope_table_raises(stores, monkeypatch):
    from botocore.exceptions import ClientError

    class _Missing:
        def get_item(self, **kwargs):
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetItem")

    monkeypatch.setattr(store, "_envelope_table", lambda: _Missing())
    with pytest.raises(ClientError):
        await store.get("org_a", "github")


@pytest.mark.asyncio
async def test_copied_row_fails_to_decrypt(stores):
    _, _, table = stores
    await store.put("org_a", "github", {"token": "t"})
    table.items[("org_b", "github")] = {**table.items[("org_a", "github")], "owner_id": "org_b"}
    with pytest.raises(ClientError):
        await store.get("org_b", "github")


@pytest.mark.asyncio
async def test_envelope_read_is_consistent(stores):
    _, _, table = stores
    await store.put("org_a", "github", {"token": "t"})
    await store.get("org_a", "github")
    assert table.consistent_reads == [True]


@pytest.mark.asyncio
async def test_delete_removes_both_copies(stores):
    secrets, _, table = stores
    secrets[LEGACY] = json.dumps({"token": "old"})
    await store.put("org_a", "github", {"token": "new"})
    assert await store.delete("org_a", "github") is True
    assert secrets == {} and table.items == {}
    assert table.deletes == [(LEGACY, True)]
    assert await store.get("org_a", "github") is None
    assert await store.delete("org_a", "github") is False


@pytest.mark.asyncio
async def test_owner_wipe_sweeps_envelopes_and_legacy_secrets(stores, monkeypatch):
    secrets, _, table = stores
    secrets[LEGACY] = json.dumps({"token": "old"})
    await store.put("org_a", "github", {"token": "a"})
    await store.put("org_b", "github", {"token": "b"})
    sm = _FakeSM(secrets, table.deletes, table.scheduled)
    monkeypatch.setattr(owner_secret_sweep.boto3, "client", lambda *a, **k: sm)
    expected = [LEGACY, "credential-envelopes/org_a/github"]
    assert await owner_secret_sweep.list_owner_secret_names("org_a") == expected
    assert await owner_secret_sweep.delete_owner_secrets("org_a") == expected
    assert secrets == {} and (LEGACY, True) in table.deletes  # force-deleted, not left recoverable
    assert list(table.items) == [("org_b", "github")]


@pytest.mark.asyncio
async def test_owner_wipe_leaves_managed_openrouter_keys_to_their_retirement(stores, monkeypatch):
    secrets, _, table = stores
    await store.put("org_a", "openrouter/h1", {"key": "sk-or-1"})
    await store.put("org_a", "github", {"token": "a"})
    monkeypatch.setattr(owner_secret_sweep.boto3, "client", lambda *a, **k: _FakeSM(secrets))
    assert await owner_secret_sweep.delete_owner_secrets("org_a") == ["credential-envelopes/org_a/github"]
    assert list(table.items) == [("org_a", "openrouter/h1")]


@pytest.mark.asyncio
async def test_oauth_app_config_is_sealed_and_its_legacy_secret_still_reads(stores):
    from core.services.credentials import oauth_app_store

    secrets, _, table = stores
    legacy = "lightbulb/dev/integrations/org_a/oauth-app/google"
    secrets[legacy] = json.dumps({"client_id": "old", "client_secret": "s"})
    assert (await oauth_app_store.get("org_a", "google"))["client_id"] == "old"
    await oauth_app_store.put("org_a", "google", client_id="cid", client_secret="csec")
    assert ("org_a", "oauth-app/google") in table.items and legacy in secrets  # left for the migration
    assert await oauth_app_store.get("org_a", "google") == {"client_id": "cid", "client_secret": "csec"}
    await oauth_app_store.delete("org_a", "google")
    assert await oauth_app_store.get("org_a", "google") is None


def test_migration_moves_per_owner_secrets_soft_deletes_dead_ones_and_verifies(stores, capsys):
    from scripts import migrate_secrets_to_envelopes as migrate

    secrets, _, table = stores
    keep = [
        "lightbulb/dev/stripe_secret_key",
        "lightbulb/dev/hermes/org_a/boot",
        "lightbulb/dev/user-keys/org_a/openai",
        "lightbulb/dev/integrations/platform-oauth/google",
        "lightbulb/prod/integrations/org_a/github",
    ]
    for name in keep:
        secrets[name] = "platform-value"
    secrets["lightbulb/dev/openrouter/owners/org_a/h1"] = "sk-or-1"
    secrets["lightbulb/dev/integrations/org_a/website-login/vault_1"] = json.dumps({"password": "pw"})
    secrets["lightbulb/dev/integrations/user_b/github"] = json.dumps({"token": "legacy"})
    secrets["lightbulb/dev/integrations/org_a/apps/app1/neon/prod"] = "dead"
    sm = store._client()
    # A newer row written after deploy must win over its legacy secret.
    table.put_item(Item=store._seal("user_b", "github", json.dumps({"token": "newer"})))

    assert migrate.run("dry-run", sm) == 0
    assert "sk-or-1" not in capsys.readouterr().out and table.deletes == []
    assert migrate.run("verify", sm) == 1  # still live
    assert migrate.run("apply", sm) == 0
    out = capsys.readouterr().out
    assert "sk-or-1" not in out and "pw" not in out and "legacy" not in out
    assert store._open(table.items[("org_a", "openrouter/h1")]) == {"key": "sk-or-1"}
    assert store._open(table.items[("org_a", "website-login/vault_1")]) == {"password": "pw"}
    assert store._open(table.items[("user_b", "github")]) == {"token": "newer"}
    # Soft-deleted (default 30-day window), never forced; platform and deferred secrets untouched.
    assert all(forced is False for _, forced in table.deletes)
    assert {n for n, _ in table.deletes} == set(secrets) - set(keep)
    assert table.scheduled == set(secrets) - set(keep)
    assert migrate.run("verify", sm) == 0


def test_migration_refuses_prod_without_allow_prod(stores, monkeypatch):
    from scripts import migrate_secrets_to_envelopes as migrate

    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    assert migrate.main(["--apply"]) == 2
    monkeypatch.setattr(migrate, "run", lambda mode: 0)
    assert migrate.main(["--apply", "--allow-prod"]) == 0
    monkeypatch.setattr(settings, "CREDENTIAL_KMS_KEY_ARN", "")
    assert migrate.main(["--dry-run"]) == 2


def test_migration_force_deletes_scheduled_secrets_only_when_their_envelope_opens(stores, capsys):
    """#5083's sealed put scheduled the legacy copy. One whose envelope opens is force-deleted; one with
    no row, or a row that does not open, is left to expire (it may be a disconnected credential)."""
    from scripts import migrate_secrets_to_envelopes as migrate

    secrets, _, table = stores
    sealed, orphan, broken = (f"lightbulb/dev/integrations/org_a/{p}" for p in ("github", "posthog", "apollo"))
    secrets.update(
        {sealed: json.dumps({"token": "old"}), orphan: json.dumps({"k": "o"}), broken: json.dumps({"k": "b"})}
    )
    table.scheduled.update({sealed, orphan, broken})
    table.put_item(Item=store._seal("org_a", "github", json.dumps({"token": "new"})))
    bad = {**store._seal("org_a", "apollo", json.dumps({"k": "x"})), "nonce": "AAAAAAAAAAAAAAAA"}
    table.put_item(Item=bad)
    sm = store._client()

    assert migrate.run("apply", sm) == 0
    assert "old" not in capsys.readouterr().out
    # Only the one whose envelope opens is force-deleted. This fake refuses it while scheduled, as the
    # DeleteSecret reference allows; with no restore it stays scheduled and expires.
    assert table.deletes == [(sealed, True)]
    assert {sealed, orphan, broken} <= table.scheduled and orphan in secrets and broken in secrets
    assert ("org_a", "posthog") not in table.items and table.items[("org_a", "apollo")] == bad
    assert migrate.run("verify", sm) == 1  # the apollo row still does not open


def test_migration_leaves_a_secret_untouched_on_a_transient_kms_error(stores, monkeypatch):
    from scripts import migrate_secrets_to_envelopes as migrate

    secrets, kms, table = stores
    name = "lightbulb/dev/integrations/org_a/github"
    secrets[name] = json.dumps({"token": "legacy"})
    newer = store._seal("org_a", "github", json.dumps({"token": "newer"}))
    table.put_item(Item=newer)

    def throttled(**_):
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "Decrypt")

    monkeypatch.setattr(kms, "decrypt", throttled)
    assert migrate.run("apply", store._client()) == 1
    assert table.items[("org_a", "github")] == newer and table.deletes == [] and name in secrets


def test_migration_skips_a_secret_written_concurrently_and_keeps_it(stores, monkeypatch):
    from scripts import migrate_secrets_to_envelopes as migrate

    secrets, _, table = stores
    name = "lightbulb/dev/integrations/org_a/github"
    secrets[name] = json.dumps({"token": "legacy"})
    live = store._seal("org_a", "github", json.dumps({"token": "live"}))
    read = migrate._row
    # The live write lands between the script's read (no row) and its put.
    monkeypatch.setattr(migrate, "_row", lambda o, p: (read(o, p), table.put_item(Item=live))[0])
    assert migrate.run("apply", store._client()) == 0
    assert table.items[("org_a", "github")] == live and table.deletes == [] and name in secrets


# Fixed vector from integration_secret_store._seal (data key bytes 0..31, nonce bytes 0..11): the gate
# (envelope.test.mjs) and settlement (test_index.py) Lambdas decrypt these exact bytes.
VECTOR = {
    "owner_id": "org_v",
    "provider": "openrouter/hv",
    "nonce": "000102030405060708090a0b",
    "ciphertext": "3c20bd7ebcc7f83baf32fca6de9b551be6b5f35b82592267ce4000954ed420ecf3126300b422e3",
}


def test_seal_matches_the_fixed_vector_the_lambdas_decrypt(stores, monkeypatch):
    import base64

    _, kms, _ = stores
    monkeypatch.setattr(
        kms, "generate_data_key", lambda **_: {"Plaintext": bytes(range(32)), "CiphertextBlob": b"wrapped"}
    )
    monkeypatch.setattr(store.os, "urandom", lambda n: bytes(range(n)))
    item = store._seal("org_v", "openrouter/hv", json.dumps({"key": "sk-or-vector"}))
    assert base64.b64decode(item["nonce"]).hex() == VECTOR["nonce"]
    assert base64.b64decode(item["ciphertext"]).hex() == VECTOR["ciphertext"]
