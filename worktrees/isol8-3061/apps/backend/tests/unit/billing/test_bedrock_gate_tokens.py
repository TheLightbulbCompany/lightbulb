"""Unit tests for core.billing.bedrock_gate_tokens — per-owner gate bearer tokens.

Covers the load-bearing invariants: hash-only storage (no plaintext ever
persisted), no Bedrock aip_tiers on a minted row, the liveness predicate that
decides when a superseded token row may finally be deleted, and the deny kill-switch toggle.
"""

import hashlib
import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from core.billing import bedrock_gate_tokens as mod


@pytest.fixture
def gate_tables(monkeypatch):
    """moto-mocked containers + gate-tokens tables."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-gate-tokens",
            KeySchema=[{"AttributeName": "token_hash", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "token_hash", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # container_repo resolves the containers table via
        # core.dynamodb.get_table("containers") -> table_name() which prepends
        # the module-level _table_prefix (cached at import). Patch it to "test-".
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", None)
        # The gate-tokens table name comes from the explicit override env var.
        monkeypatch.setenv("BEDROCK_GATE_TOKENS_TABLE", "test-gate-tokens")
        yield client


def _put_container_row(client, owner_id: str, *, superseded: list[str] | None = None, **attrs) -> None:
    item = {
        "owner_id": {"S": owner_id},
        "id": {"S": "c-1"},
        "created_at": {"S": "2026-07-15T00:00:00Z"},
        # The drain reads this to find the runtime whose boot secret it interrogates. A row
        # without one presents nothing (see _presentable_gate_token_hashes); tests that want
        # that case pass agentcore_runtime_arn="".
        "agentcore_runtime_arn": {"S": _RUNTIME_ARN},
    }
    for k, v in attrs.items():
        item[k] = {"S": v}
    if superseded is not None:
        item[mod._SUPERSEDED_FIELD] = {"L": [{"S": h} for h in superseded]}
    client.put_item(TableName="test-containers", Item=item)


def _token_rows(client) -> list[dict]:
    return client.scan(TableName="test-gate-tokens")["Items"]


def _put_token_row(client, token_hash: str, owner_id: str = "org_abc", *, ttl: int | None = None) -> None:
    item = {
        "token_hash": {"S": token_hash},
        "owner_id": {"S": owner_id},
        "denied": {"BOOL": False},
    }
    if ttl is not None:
        # The legacy one-hour rotation grace. Deleting the constant that wrote
        # these does not delete the attributes already on rows.
        item["ttl"] = {"N": str(ttl)}
    client.put_item(TableName="test-gate-tokens", Item=item)


def _token_row(client, token_hash: str) -> dict:
    return client.get_item(TableName="test-gate-tokens", Key={"token_hash": {"S": token_hash}}).get("Item") or {}


def _container_row(client, owner_id: str = "org_abc") -> dict:
    return client.get_item(TableName="test-containers", Key={"owner_id": {"S": owner_id}})["Item"]


def _tracked(client, owner_id: str = "org_abc") -> list[str]:
    row = _container_row(client, owner_id)
    return [v["S"] for v in row.get(mod._SUPERSEDED_FIELD, {}).get("L", [])]


# ---------------------------------------------------------------------------
# The owner's runtime, as the drain predicate sees it: one ``GetAgentRuntime``
# plus the boot secret it names. Scenarios are expressed as runtime state and
# the assertions are on the real DynamoDB rows that survive.
# ---------------------------------------------------------------------------

_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/lightbulb_prod_org_abc-r1"
_BOOT_SECRET = "lightbulb/dev/hermes/org_abc/boot"


class FakeRuntime:
    """Stand-in for the two AWS reads the predicate makes.

    ``bearer`` is the PLAINTEXT the runtime's boot secret currently carries -- i.e. the one
    token a session launched from it can present. ``secret_id=None`` is the positive finding
    "this runtime names no boot secret"; ``raises`` is the unknown one.
    """

    def __init__(self, *, bearer: str | None = None, secret_id: str | None = _BOOT_SECRET):
        self.bearer = bearer
        self.secret_id = secret_id
        self.raises: Exception | None = None
        self.secret_string: str | None = None  # set to override the JSON body wholesale
        self.calls: list[str] = []

    def describe(self, runtime_arn):
        self.calls.append("get_agent_runtime")
        if self.raises:
            raise self.raises
        env = {"LIGHTBULB_SECRET_ID": self.secret_id} if self.secret_id else {}
        return {"environmentVariables": env}

    def secret(self, name):
        self.calls.append(f"get_secret_value:{name}")
        if self.secret_string is not None:
            return self.secret_string
        return json.dumps({"hookToken": "h", "gatewayToken": "g", "bedrockBearer": self.bearer or ""})


@pytest.fixture
def fake_runtime(monkeypatch):
    """Install a FakeRuntime; the test mutates it before calling into the module."""
    runtime = FakeRuntime()
    monkeypatch.setattr(mod, "_describe_agent_runtime", runtime.describe)
    monkeypatch.setattr(mod, "_secret_value", runtime.secret)
    return runtime


class TestMint:
    @pytest.mark.asyncio
    async def test_logs_owner_and_both_hash_prefixes(self, gate_tables, fake_runtime, caplog):
        """The mint log line is the whole point of this module: it must carry
        enough to correlate against a `describe-task-definition` without
        logging anything a reader could turn back into a working credential."""
        old_hash = mod.hash_token("old-plaintext")
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        _put_token_row(gate_tables, old_hash)
        fake_runtime.bearer = "old-plaintext"

        with caplog.at_level("INFO", logger=mod.logger.name):
            token = await mod.mint_gate_token("org_abc", row_id="c-1")

        new_hash = mod.hash_token(token)
        mint_records = [r for r in caplog.records if "minted" in r.message]
        assert len(mint_records) == 1
        message = mint_records[0].message
        assert "org_abc" in message
        assert new_hash[: mod._HASH_PREFIX_LEN] in message
        assert old_hash[: mod._HASH_PREFIX_LEN] in message
        # Full hashes and the plaintext must never appear.
        assert new_hash not in message
        assert old_hash not in message
        assert token not in message

    @pytest.mark.asyncio
    async def test_logs_none_for_a_first_mint_with_no_prior_token(self, gate_tables, fake_runtime, caplog):
        _put_container_row(gate_tables, "org_abc")

        with caplog.at_level("INFO", logger=mod.logger.name):
            await mod.mint_gate_token("org_abc", row_id="c-1")

        mint_records = [r for r in caplog.records if "minted" in r.message]
        assert len(mint_records) == 1
        assert "prior=none" in mint_records[0].message

    @pytest.mark.asyncio
    async def test_no_captured_log_output_contains_the_plaintext_token(self, gate_tables, fake_runtime, caplog):
        """The constraint most likely to regress: assert it explicitly rather
        than trust review to catch a future `logger.debug(token)`."""
        old_hash = mod.hash_token("old-plaintext")
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        _put_token_row(gate_tables, old_hash)
        fake_runtime.bearer = "old-plaintext"

        with caplog.at_level("DEBUG"):
            token = await mod.mint_gate_token("org_abc", row_id="c-1")
            await mod.expire_superseded_gate_tokens("org_abc")

        for record in caplog.records:
            assert token not in record.getMessage()
            assert "old-plaintext" not in record.getMessage()

    @pytest.mark.asyncio
    async def test_writes_hash_only_row_without_aip_tiers(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")

        token = await mod.mint_gate_token("org_abc", row_id="c-1")

        rows = _token_rows(gate_tables)
        assert len(rows) == 1
        row = rows[0]
        # PK is the SHA-256 hex of the returned plaintext.
        assert row["token_hash"]["S"] == hashlib.sha256(token.encode()).hexdigest()
        assert row["owner_id"]["S"] == "org_abc"
        assert row["denied"]["BOOL"] is False
        assert "created_at" in row
        # No Bedrock tier map: the retired bedrock-gate refuses every model this bearer names.
        assert "aip_tiers" not in row

    @pytest.mark.asyncio
    async def test_no_plaintext_token_is_persisted_anywhere(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")

        token = await mod.mint_gate_token("org_abc", row_id="c-1")

        # The plaintext must appear in NO stored attribute (hash-only storage).
        for row in _token_rows(gate_tables):
            for av in row.values():
                assert av.get("S") != token
        # And not on the containers row either — only the hash pointer.
        crow = gate_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert crow["bedrock_gate_token_hash"]["S"] == hashlib.sha256(token.encode()).hexdigest()
        for av in crow.values():
            assert av.get("S") != token

    @pytest.mark.asyncio
    async def test_points_containers_row_at_new_hash(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")

        token = await mod.mint_gate_token("org_abc", row_id="c-1")

        crow = gate_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert crow["bedrock_gate_token_hash"]["S"] == mod.hash_token(token)

    @pytest.mark.asyncio
    async def test_minting_never_puts_the_prior_row_on_a_clock(self, gate_tables, fake_runtime):
        """THE INCIDENT, at its source: a mint must schedule NOTHING.

        The prior row used to get a 3600s ttl here. When the redeploy that would
        have delivered the new token never landed, DynamoDB reaped the token the
        live container was still presenting and killed it permanently.
        """
        # The service still references the def carrying the old token — nothing
        # has rolled, so the mint must leave that row untouched and unexpiring.
        old_hash = mod.hash_token("old-plaintext")
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        _put_token_row(gate_tables, old_hash)
        fake_runtime.bearer = "old-plaintext"

        token = await mod.mint_gate_token("org_abc", row_id="c-1")

        rows = {r["token_hash"]["S"]: r for r in _token_rows(gate_tables)}
        assert old_hash in rows and mod.hash_token(token) in rows
        # No row carries a ttl — nothing is on a fuse anymore.
        assert all("ttl" not in r for r in rows.values())
        # The superseded hash is TRACKED instead, for the predicate to judge later.
        assert _tracked(gate_tables) == [old_hash]

    @pytest.mark.asyncio
    async def test_a_warm_sessions_old_bearer_survives_every_mint_until_a_confirmed_stop(
        self, gate_tables, fake_runtime
    ):
        """C1 (Task 8a fix round 1): a pre-derivation owner's first reconcile mints the derived
        bearer and rewrites the boot secret; a SECOND mint (the runner's two prepares) then saw
        the old hash as unpresentable -- the secret no longer carries it -- and deleted the
        token row a warm microVM was still presenting. The mint never deletes now; only the
        expiry after a confirmed session stop does."""
        old_plaintext = "random-pre-derivation-bearer"
        old_hash = mod.hash_token(old_plaintext)
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        _put_token_row(gate_tables, old_hash)
        fake_runtime.bearer = old_plaintext

        token = await mod.mint_gate_token("org_abc", row_id="c-1")
        fake_runtime.bearer = token  # prepare rewrote the boot secret; the warm session did not reload
        assert await mod.mint_gate_token("org_abc", row_id="c-1") == token

        assert old_hash in {r["token_hash"]["S"] for r in _token_rows(gate_tables)}
        assert _tracked(gate_tables) == [old_hash]

        await mod.expire_superseded_gate_tokens("org_abc")  # after StopRuntimeSession
        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {mod.hash_token(token)}

    @pytest.mark.asyncio
    async def test_a_bumped_generation_rotates_the_token(self, gate_tables, fake_runtime):
        _put_container_row(gate_tables, "org_abc")
        t1 = await mod.mint_gate_token("org_abc", row_id="c-1")
        t2 = await mod.mint_gate_token("org_abc", row_id="c-1", generation=1)
        assert t1 != t2
        # The superseded token OUTLIVES the mint by design — it is tracked, not
        # scheduled — and only goes when the predicate proves it unreachable.
        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {
            mod.hash_token(t1),
            mod.hash_token(t2),
        }
        assert _tracked(gate_tables) == [mod.hash_token(t1)]

        fake_runtime.secret_id = None  # the runtime names no boot secret: nothing holds t1
        await mod.expire_superseded_gate_tokens("org_abc")

        assert [r["token_hash"]["S"] for r in _token_rows(gate_tables)] == [mod.hash_token(t2)]

    @pytest.mark.asyncio
    async def test_the_same_generation_is_the_same_token_and_a_second_mint_changes_nothing(
        self, gate_tables, fake_runtime
    ):
        """Two provision runs for one owner must converge, not rotate: the bearer is
        derived, the token row is create-only (a deny survives), and the pointer
        already names the hash so nothing is superseded."""
        _put_container_row(gate_tables, "org_abc", agentcore_runtime_arn="")
        t1 = await mod.mint_gate_token("org_abc", row_id="c-1")
        await mod.deny_gate_token("org_abc", True)

        t2 = await mod.mint_gate_token("org_abc", row_id="c-1")

        assert t1 == t2 == mod.derive_gate_bearer("org_abc", "c-1", 0)
        rows = _token_rows(gate_tables)
        assert [r["token_hash"]["S"] for r in rows] == [mod.hash_token(t1)]
        assert rows[0]["denied"]["BOOL"] is True  # the second run did not un-deny it
        assert _container_row(gate_tables)["bedrock_gate_token_hash"]["S"] == mod.hash_token(t1)
        assert _tracked(gate_tables) == []


class TestDeriveGateBearer:
    def test_stable_per_owner_row_and_generation(self):
        assert mod.derive_gate_bearer("org_a", "row-1", 0) == mod.derive_gate_bearer("org_a", "row-1", 0)

    def test_differs_across_generation_row_and_owner(self):
        tokens = {
            mod.derive_gate_bearer("org_a", "row-1", 0),
            mod.derive_gate_bearer("org_a", "row-1", 1),  # explicit rotation
            mod.derive_gate_bearer("org_a", "row-2", 0),  # teardown + reprovision: a new row id
            mod.derive_gate_bearer("org_b", "row-1", 0),
        }
        assert len(tokens) == 4

    def test_is_hmac_sha256_of_the_versioned_label_keyed_by_the_dedicated_key(self, monkeypatch):
        import base64
        import hmac

        monkeypatch.setattr(mod.settings, "GATE_BEARER_HMAC_KEY", "k")
        monkeypatch.setattr(mod.settings, "ENCRYPTION_KEY", "other")
        want = base64.urlsafe_b64encode(
            hmac.new(b"k", b"lightbulb-gate-bearer:v1:org_a:row-1:3", hashlib.sha256).digest()
        ).rstrip(b"=")
        assert mod.derive_gate_bearer("org_a", "row-1", 3) == want.decode()

    def test_never_depends_on_encryption_key(self, monkeypatch):
        before = mod.derive_gate_bearer("org_a", "row-1", 0)
        monkeypatch.setattr(mod.settings, "ENCRYPTION_KEY", "rotated")
        assert mod.derive_gate_bearer("org_a", "row-1", 0) == before

    def test_refuses_an_empty_key_with_no_fallback(self, monkeypatch):
        """An empty HMAC key would make every bearer computable from public ids."""
        monkeypatch.setattr(mod.settings, "GATE_BEARER_HMAC_KEY", "")
        with pytest.raises(RuntimeError):
            mod.derive_gate_bearer("org_a", "row-1", 0)

    def test_refuses_an_empty_row_id(self):
        with pytest.raises(ValueError):
            mod.derive_gate_bearer("org_a", "", 0)


class TestConcurrentMints:
    """Two mints for the same owner, both reading before either writes.

    Reachable in production: the AIP/role/token mint block in
    ``provision_user_container`` sits AHEAD of the first
    ``_provision_checkpoint``, so the provision lease does not serialize it —
    a poll provision and an escalating ``container_recover`` takeover can both
    be inside it at once.
    """

    @staticmethod
    def _interleave(monkeypatch, owner_id: str = "org_abc", generation: int = 1) -> dict:
        """Force mint B to run to completion between mint A's read and its write.

        No sleeps and no real concurrency: the wrapper runs the whole second
        mint on the first entry to the row write, which is exactly the ordering
        "A reads, B reads + writes, A writes".
        """
        from core.repositories import container_repo

        real_swap = container_repo.swap_gate_token_hash
        state: dict = {}

        async def swap(*args, **kwargs):
            if not state.get("started"):
                state["started"] = True  # set BEFORE the await, or B recurses
                state["token_b"] = await mod.mint_gate_token(owner_id, row_id="c-1", generation=generation)
            return await real_swap(*args, **kwargs)

        monkeypatch.setattr(container_repo, "swap_gate_token_hash", swap)
        return state

    @pytest.mark.asyncio
    async def test_both_minted_hashes_stay_reachable(self, gate_tables, fake_runtime, monkeypatch):
        """Neither mint may erase the other's hash from the row.

        The row is the ONLY handle on a gate-token row (hash-keyed table, no
        owner index), so a hash dropped here is a denied=False credential that
        can never be expired or denied again.
        """
        _put_container_row(gate_tables, "org_abc")
        state = self._interleave(monkeypatch)

        token_a = await mod.mint_gate_token("org_abc", row_id="c-1")

        hash_a, hash_b = mod.hash_token(token_a), mod.hash_token(state["token_b"])
        assert hash_a != hash_b
        row = _container_row(gate_tables)
        reachable = {row["bedrock_gate_token_hash"]["S"], *_tracked(gate_tables)}
        assert reachable == {hash_a, hash_b}
        # Both token rows still exist — the loser was tracked, not orphaned.
        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {hash_a, hash_b}

    @pytest.mark.asyncio
    async def test_two_same_generation_mints_converge_on_one_row_and_one_pointer(
        self, gate_tables, fake_runtime, monkeypatch
    ):
        """The ordinary shape once leases go: two provision runs, same generation."""
        _put_container_row(gate_tables, "org_abc", agentcore_runtime_arn="")
        state = self._interleave(monkeypatch, generation=0)

        token_a = await mod.mint_gate_token("org_abc", row_id="c-1")

        assert token_a == state["token_b"]
        assert [r["token_hash"]["S"] for r in _token_rows(gate_tables)] == [mod.hash_token(token_a)]
        assert _container_row(gate_tables)["bedrock_gate_token_hash"]["S"] == mod.hash_token(token_a)
        assert _tracked(gate_tables) == []

    @pytest.mark.asyncio
    async def test_deny_reaches_the_race_loser(self, gate_tables, fake_runtime, monkeypatch):
        """THE CONSEQUENCE. An untracked hash is a live bearer credential the
        trial-cap backstop and admin revoke cannot kill."""
        _put_container_row(gate_tables, "org_abc")
        state = self._interleave(monkeypatch)

        token_a = await mod.mint_gate_token("org_abc", row_id="c-1")

        await mod.deny_gate_token("org_abc", True)

        denied = {r["token_hash"]["S"]: r["denied"]["BOOL"] for r in _token_rows(gate_tables)}
        assert denied == {mod.hash_token(token_a): True, mod.hash_token(state["token_b"]): True}

    @pytest.mark.asyncio
    async def test_retry_exhaustion_logs_and_does_not_raise_or_re_mint(
        self, gate_tables, fake_runtime, monkeypatch, caplog
    ):
        """Provisioning must not fail over a tracking gap — and the retry must
        re-run the WRITE only: a re-mint per attempt would leak exactly the
        unreachable token row this fix exists to prevent."""
        from core.repositories import container_repo

        attempts = []

        async def always_conflict(*args, **kwargs):
            attempts.append(kwargs.get("expected_prior_hash"))
            return "conflict", {"owner_id": "org_abc", "bedrock_gate_token_hash": "1" * 64}

        _put_container_row(gate_tables, "org_abc")
        monkeypatch.setattr(container_repo, "swap_gate_token_hash", always_conflict)

        with caplog.at_level("ERROR", logger=mod.logger.name):
            token = await mod.mint_gate_token("org_abc", row_id="c-1")

        assert len(attempts) == 3  # bounded
        # ONE token row: the mint happened once, ahead of the loop.
        assert [r["token_hash"]["S"] for r in _token_rows(gate_tables)] == [mod.hash_token(token)]
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "org_abc" in errors[0].getMessage()
        assert mod.hash_token(token)[: mod._HASH_PREFIX_LEN] in errors[0].getMessage()
        assert mod.hash_token(token) not in errors[0].getMessage()
        assert token not in errors[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_conflict_is_never_misread_as_absent_by_a_stale_replica(
        self, gate_tables, fake_runtime, monkeypatch
    ):
        """The fresh-provision shape, with the two preconditions CORRELATED.

        The mint reads no row, so its swap condition is
        attribute_not_exists(bedrock_gate_token_hash). A concurrent run's
        create_user_service upsert then creates the row, and the swap is refused
        on the leader — a genuine conflict. But classifying it with a follow-up
        get_item is an eventually-consistent read of a seconds-old creation, so
        it can answer "no row": the conflict would be reported as the benign
        fresh-provision no-op, the mint would break out WITHOUT retrying and
        WITHOUT an error, and its hash would be orphaned silently.

        Here get_by_owner_id is made permanently blind from the moment the row
        appears. The mint must still land, because the conflict state comes off
        the refused write itself.
        """
        from core.repositories import container_repo

        rival_plaintext = "rival-token"
        rival_hash = mod.hash_token(rival_plaintext)
        real_get = container_repo.get_by_owner_id
        real_swap = container_repo.swap_gate_token_hash
        state: dict = {}

        async def stale_get(owner_id):
            return None if state.get("created") else await real_get(owner_id)

        async def swap(*args, **kwargs):
            if not state.get("created"):
                state["created"] = True
                _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=rival_hash)
                _put_token_row(gate_tables, rival_hash)
            return await real_swap(*args, **kwargs)

        monkeypatch.setattr(container_repo, "get_by_owner_id", stale_get)
        monkeypatch.setattr(container_repo, "swap_gate_token_hash", swap)

        token = await mod.mint_gate_token("org_abc", row_id="c-1")

        row = _container_row(gate_tables)
        assert row["bedrock_gate_token_hash"]["S"] == mod.hash_token(token)
        assert _tracked(gate_tables) == [rival_hash]
        monkeypatch.setattr(container_repo, "get_by_owner_id", real_get)  # replica catches up
        await mod.deny_gate_token("org_abc", True)
        assert {r["token_hash"]["S"]: r["denied"]["BOOL"] for r in _token_rows(gate_tables)} == {
            mod.hash_token(token): True,
            rival_hash: True,
        }

    @pytest.mark.asyncio
    async def test_absent_row_stays_a_silent_no_op(self, gate_tables, fake_runtime, caplog):
        """The fresh-provision path: no containers row yet. The swap must not
        create one (dispatch does that) and must not look
        like a lost race."""
        with caplog.at_level("ERROR", logger=mod.logger.name):
            token = await mod.mint_gate_token("org_new", row_id="c-1")

        assert [r["token_hash"]["S"] for r in _token_rows(gate_tables)] == [mod.hash_token(token)]
        assert "Item" not in gate_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_new"}})
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []


class TestLegacyTtlDisarm:
    """Deploying this branch must not REPRODUCE the incident it fixes.

    The module replaced a flat one-hour TTL with a liveness predicate, but
    deleting the constant does not delete the ``ttl`` attributes already written
    onto rows. Each is a live clock: DynamoDB reaps the row out from under the
    container presenting it, and its next inference call gets
    UnrecognizedClientException. THE INVARIANT: no token row anything can
    present may carry a clock-based expiry.
    """

    _FUTURE_TTL = 4102444800  # 2100-01-01; a clock that has NOT yet fired

    @pytest.mark.asyncio
    async def test_the_legacy_failed_roll_state_is_adopted_and_disarmed(self, gate_tables, fake_runtime):
        """THE STATE WE KNOW IS IN PROD — it is what this PR was written about.

        The service's task definition presents A, the containers pointer is B,
        nothing tracks A, and A's row still carries the old ttl. The new list is
        built from the containers row, so A is invisible to it: never
        considered, never disarmed, and reaped on the old clock.
        """
        a_plaintext = "legacy-token-A"
        hash_a = mod.hash_token(a_plaintext)
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash="hash_B")
        _put_token_row(gate_tables, "hash_B")
        _put_token_row(gate_tables, hash_a, ttl=self._FUTURE_TTL)
        fake_runtime.bearer = a_plaintext  # the service presents A

        await mod.mint_gate_token("org_abc", row_id="c-1")

        # Adopted: tracked, so the predicate protects it and deny reaches it.
        assert hash_a in _tracked(gate_tables)
        row_a = _token_row(gate_tables, hash_a)
        assert row_a, "the adopted row must survive the drain"
        assert "ttl" not in row_a, "the legacy clock is still armed on a row we now promise to keep"

    @pytest.mark.asyncio
    async def test_a_rolling_deploy_ttl_is_disarmed_as_the_hash_is_tracked(self, gate_tables, fake_runtime):
        """The rollout window: an OLD backend task mints and TTLs the outgoing
        row, then a NEW task tracks that hash. Tracking must disarm it, or the
        hash is protected by the predicate and reaped by DynamoDB at once."""
        old_plaintext = "outgoing-token"
        old_hash = mod.hash_token(old_plaintext)
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        _put_token_row(gate_tables, old_hash, ttl=self._FUTURE_TTL)  # the old task's grace clock
        fake_runtime.bearer = old_plaintext

        await mod.mint_gate_token("org_abc", row_id="c-1")

        assert _tracked(gate_tables) == [old_hash]
        assert "ttl" not in _token_row(gate_tables, old_hash)

    @pytest.mark.asyncio
    async def test_a_row_without_a_ttl_is_left_exactly_as_it_was(self, gate_tables, fake_runtime):
        """No spurious writes: the disarm is conditional on the attribute
        existing, so the ordinary post-cutover row takes none. Matters more now
        the disarm rides the REPEATED retention path, not a one-shot."""
        old_plaintext = "outgoing-token"
        old_hash = mod.hash_token(old_plaintext)
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        _put_token_row(gate_tables, old_hash)
        fake_runtime.bearer = old_plaintext
        before = _token_row(gate_tables, old_hash)

        await mod.mint_gate_token("org_abc", row_id="c-1")
        for _ in range(3):  # every wake re-evaluates retention
            await mod.expire_superseded_gate_tokens("org_abc")

        assert _token_row(gate_tables, old_hash) == before

    @pytest.mark.asyncio
    async def test_a_ttl_armed_after_adoption_is_still_removed(self, gate_tables, fake_runtime):
        """P1a — THE LATE WRITE. A one-shot disarm loses to anything that arms
        the clock afterwards.

        Mid-rollout an old task moves the pointer off A, pauses, and only THEN
        issues its unconditional `SET ttl` on A — after the new task has already
        adopted A and looked for a ttl that was not yet there. Retention is
        evaluated every drain, so the next one takes it off.
        """
        a_plaintext = "legacy-token-A"
        hash_a = mod.hash_token(a_plaintext)
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash="hash_B")
        _put_token_row(gate_tables, "hash_B")
        _put_token_row(gate_tables, hash_a)  # NO ttl yet — nothing to disarm
        fake_runtime.bearer = a_plaintext

        await mod.mint_gate_token("org_abc", row_id="c-1")
        assert hash_a in _tracked(gate_tables)
        assert "ttl" not in _token_row(gate_tables, hash_a)

        # The old task resumes and arms the clock on a hash we now protect.
        _put_token_row(gate_tables, hash_a, ttl=self._FUTURE_TTL)
        await mod.expire_superseded_gate_tokens("org_abc")

        assert "ttl" not in _token_row(gate_tables, hash_a), "a late-armed clock survived the retention"
        assert _token_row(gate_tables, hash_a), "and the row itself must still be there"

    @pytest.mark.asyncio
    async def test_a_disarm_that_failed_is_retried_and_not_reported_as_done(
        self, gate_tables, fake_runtime, monkeypatch, caplog
    ):
        """P1b — A THROTTLED DISARM. The mint must not record the hash as
        protected while quietly leaving its clock armed, and must not abort the
        provision either: the next retention retries it."""
        old_plaintext = "outgoing-token"
        old_hash = mod.hash_token(old_plaintext)
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        _put_token_row(gate_tables, old_hash, ttl=self._FUTURE_TTL)
        fake_runtime.bearer = old_plaintext

        # Throttle the REMOVE itself, so the real _disarm_ttl's own handling is
        # what runs — the point is that it swallows without claiming success.
        real_run_in_thread = mod.run_in_thread
        throttled: list[str] = []

        async def throttle_first_update(fn, *args, **kwargs):
            if getattr(fn, "__name__", "") == "update_item" and not throttled:
                throttled.append("x")
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "UpdateItem"
                )
            return await real_run_in_thread(fn, *args, **kwargs)

        monkeypatch.setattr(mod, "run_in_thread", throttle_first_update)
        with caplog.at_level("WARNING", logger=mod.logger.name):
            await mod.mint_gate_token("org_abc", row_id="c-1")  # completes despite the failure

        assert throttled, "the disarm never ran"
        assert _tracked(gate_tables) == [old_hash]
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "STILL ARMED" in messages
        assert "disarmed" not in messages, "a failure must never read as a success"
        assert "ttl" in _token_row(gate_tables, old_hash), "precondition: the failed disarm left it armed"

        monkeypatch.setattr(mod, "run_in_thread", real_run_in_thread)
        await mod.expire_superseded_gate_tokens("org_abc")
        assert "ttl" not in _token_row(gate_tables, old_hash)

    @pytest.mark.asyncio
    async def test_an_adopted_hash_is_an_ORDINARY_tracked_hash(self, gate_tables, fake_runtime):
        """Adopt + disarm is all it needs: the predicate keeps it while the
        service presents it, deny_gate_token reaches it, and it expires the
        moment nothing can present it — exactly like a natively-tracked hash."""
        a_plaintext = "legacy-token-A"
        hash_a = mod.hash_token(a_plaintext)
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash="hash_B")
        _put_token_row(gate_tables, "hash_B")
        _put_token_row(gate_tables, hash_a, ttl=self._FUTURE_TTL)
        fake_runtime.bearer = a_plaintext

        await mod.mint_gate_token("org_abc", row_id="c-1")

        # The kill switch reaches it.
        await mod.deny_gate_token("org_abc", True)
        assert _token_row(gate_tables, hash_a)["denied"]["BOOL"] is True

        # And it expires once nothing presents it — no clock needed.
        fake_runtime.secret_id = None
        await mod.expire_superseded_gate_tokens("org_abc")
        assert _token_row(gate_tables, hash_a) == {}


class TestExpireSuperseded:
    """The liveness predicate: a token row lives until nothing can present it.

    A session gets its bearer from the ONE boot secret the owner's runtime names
    (``LIGHTBULB_SECRET_ID`` -> ``{hookToken, gatewayToken, bedrockBearer}``), read at boot, so
    the authoritative question is what that secret carries right now -- what the next session
    will present. One runtime per workspace, so that is one ``GetAgentRuntime`` and one
    ``GetSecretValue``; there is no task enumeration and no second place a bearer can hide.

    THE INCIDENT this module exists for is unchanged in shape: expiring a token the running
    workspace is still presenting kills its next inference call with
    ``UnrecognizedClientException``, permanently. So unknown must mean keep.
    """

    OLD_PLAINTEXT = "token-A"
    NEW_PLAINTEXT = "token-B"

    def _arrange(self, client, fake_runtime, *, presents):
        """Owner mid-rotation: current token B, superseded token A still tracked.

        ``presents`` is the plaintext the runtime's boot secret currently carries.
        """
        old_hash = mod.hash_token(self.OLD_PLAINTEXT)
        new_hash = mod.hash_token(self.NEW_PLAINTEXT)
        _put_container_row(
            client,
            "org_abc",
            bedrock_gate_token_hash=new_hash,
            superseded=[old_hash],
        )
        _put_token_row(client, old_hash)
        _put_token_row(client, new_hash)
        fake_runtime.bearer = presents
        return old_hash, new_hash

    @pytest.mark.asyncio
    async def test_normal_rotation_expires_the_superseded_token(self, gate_tables, fake_runtime):
        """The boot secret carries B -- nothing can present A any more."""
        _old_hash, new_hash = self._arrange(gate_tables, fake_runtime, presents=self.NEW_PLAINTEXT)

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {new_hash}

    @pytest.mark.asyncio
    async def test_a_boot_secret_still_carrying_the_old_bearer_keeps_it(self, gate_tables, fake_runtime):
        """THE INCIDENT. The rotation never landed in the secret, so the next session boots
        holding A. Expiring A here is what killed a paying customer's container for good --
        and it is invisible until that session's first inference call."""
        old_hash, new_hash = self._arrange(gate_tables, fake_runtime, presents=self.OLD_PLAINTEXT)

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_a_parked_workspace_keeps_its_token(self, gate_tables, fake_runtime):
        """No live session at all -- an idle trial workspace whose microVM was reaped. The
        secret still carries A, so A must survive an idle week and be there on the next wake.
        "Expire when no session holds it" would delete it right here."""
        old_hash, _new_hash = self._arrange(gate_tables, fake_runtime, presents=self.OLD_PLAINTEXT)

        await mod.expire_superseded_gate_tokens("org_abc")

        assert old_hash in {r["token_hash"]["S"] for r in _token_rows(gate_tables)}

    @pytest.mark.asyncio
    async def test_a_runtime_naming_no_boot_secret_expires_the_token(self, gate_tables, fake_runtime):
        """A POSITIVE finding, not missing data: the runtime's environment names no boot
        secret, so no session launched from it can present anything."""
        _old_hash, new_hash = self._arrange(gate_tables, fake_runtime, presents=self.OLD_PLAINTEXT)
        fake_runtime.secret_id = None

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {new_hash}

    @pytest.mark.asyncio
    async def test_a_row_with_no_runtime_yet_expires_the_token(self, gate_tables, fake_runtime):
        """Mid-provision, or a deleted workspace: there is no runtime, so nothing will ever
        launch with it."""
        old_hash = mod.hash_token(self.OLD_PLAINTEXT)
        new_hash = mod.hash_token(self.NEW_PLAINTEXT)
        _put_container_row(
            gate_tables,
            "org_abc",
            bedrock_gate_token_hash=new_hash,
            superseded=[old_hash],
            agentcore_runtime_arn="",
        )
        _put_token_row(gate_tables, old_hash)
        _put_token_row(gate_tables, new_hash)

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {new_hash}
        assert fake_runtime.calls == []  # a row with no ARN is answered without an AWS read

    @pytest.mark.asyncio
    async def test_an_unreadable_runtime_keeps_the_token_and_does_not_raise(self, gate_tables, fake_runtime):
        """Fail toward availability: an unprovable token is a LIVE token."""
        old_hash, new_hash = self._arrange(gate_tables, fake_runtime, presents=self.NEW_PLAINTEXT)
        fake_runtime.raises = RuntimeError("ThrottlingException: Rate exceeded")

        await mod.expire_superseded_gate_tokens("org_abc")  # must not raise

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_a_malformed_boot_secret_keeps_the_token(self, gate_tables, fake_runtime):
        """The other unknown. A body that is not the fixed {hookToken, gatewayToken,
        bedrockBearer} shape is incomplete data, never proof the runtime holds no bearer --
        and reading it as empty IS the deletion decision."""
        old_hash, new_hash = self._arrange(gate_tables, fake_runtime, presents=self.NEW_PLAINTEXT)
        fake_runtime.secret_string = "not-json"

        await mod.expire_superseded_gate_tokens("org_abc")  # must not raise

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_a_dynamodb_failure_does_not_raise(self, gate_tables, fake_runtime, monkeypatch):
        self._arrange(gate_tables, fake_runtime, presents=self.NEW_PLAINTEXT)

        async def boom(*_a, **_kw):
            raise RuntimeError("ProvisionedThroughputExceeded")

        monkeypatch.setattr(mod.container_repo, "get_by_owner_id", boom)

        await mod.expire_superseded_gate_tokens("org_abc")  # must not raise

    @pytest.mark.asyncio
    async def test_never_deletes_the_owners_current_token(self, gate_tables, fake_runtime):
        """Defensive: if the CURRENT hash ever lands in the tracked list, it is not superseded
        and must never be a deletion candidate -- even though the boot secret does not carry it
        yet (a fresh mint before the runtime is updated)."""
        current = mod.hash_token(self.NEW_PLAINTEXT)
        _put_container_row(
            gate_tables,
            "org_abc",
            bedrock_gate_token_hash=current,
            superseded=[current],
        )
        _put_token_row(gate_tables, current)
        fake_runtime.bearer = self.OLD_PLAINTEXT

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {current}
        assert fake_runtime.calls == []  # not even evaluated

    @pytest.mark.asyncio
    async def test_non_bedrock_owner_is_never_evaluated(self, gate_tables, fake_runtime):
        """chatgpt_oauth / byo_key owners never mint a gate token, so they carry no hash and no
        tracked list -- the path must stop before touching AWS at all."""
        _put_container_row(gate_tables, "org_oauth", provider_choice="chatgpt_oauth")

        await mod.expire_superseded_gate_tokens("org_oauth")

        assert fake_runtime.calls == []
        assert _token_rows(gate_tables) == []

    @pytest.mark.asyncio
    async def test_missing_container_row_is_a_noop(self, gate_tables, fake_runtime):
        await mod.expire_superseded_gate_tokens("ghost")
        assert fake_runtime.calls == []

    @pytest.mark.asyncio
    async def test_kept_verdict_names_what_is_holding_the_token(self, gate_tables, fake_runtime, caplog):
        """THE INCIDENT scenario again, but asserting the log line this time: a bare 'kept'
        would have left the same blind spot the incident did. The reason has to name the
        runtime AND the secret, because those are the two things an operator can go read."""
        old_hash, _ = self._arrange(gate_tables, fake_runtime, presents=self.OLD_PLAINTEXT)

        with caplog.at_level("INFO", logger=mod.logger.name):
            await mod.expire_superseded_gate_tokens("org_abc")

        kept = [r.message for r in caplog.records if "kept" in r.message]
        assert len(kept) == 1
        assert "org_abc" in kept[0]
        assert old_hash[: mod._HASH_PREFIX_LEN] in kept[0]
        assert old_hash not in kept[0]  # never the whole hash
        assert _BOOT_SECRET in kept[0]

    @pytest.mark.asyncio
    async def test_expired_verdict_logs_its_reason(self, gate_tables, fake_runtime, caplog):
        old_hash, _ = self._arrange(gate_tables, fake_runtime, presents=self.NEW_PLAINTEXT)

        with caplog.at_level("INFO", logger=mod.logger.name):
            await mod.expire_superseded_gate_tokens("org_abc")

        expired = [r.message for r in caplog.records if "expired" in r.message]
        assert len(expired) == 1
        assert "org_abc" in expired[0]
        assert old_hash[: mod._HASH_PREFIX_LEN] in expired[0]
        assert old_hash not in expired[0]

    @pytest.mark.asyncio
    async def test_expiry_logs_once_not_on_every_later_evaluation(self, gate_tables, fake_runtime, caplog):
        """MINOR 4. The survivor list is deliberately not written back, so the hash stays
        tracked until the next mint and every later wake re-runs the predicate and re-issues
        the delete. Only the evaluation that actually REMOVED the row may log 'expired' --
        otherwise N re-confirmations are indistinguishable from N expiries in the logs."""
        self._arrange(gate_tables, fake_runtime, presents=self.NEW_PLAINTEXT)

        with caplog.at_level("INFO", logger=mod.logger.name):
            await mod.expire_superseded_gate_tokens("org_abc")
            await mod.expire_superseded_gate_tokens("org_abc")
            await mod.expire_superseded_gate_tokens("org_abc")

        assert len([r for r in caplog.records if "expired" in r.message]) == 1

    @pytest.mark.asyncio
    async def test_reads_the_bedrock_bearer_key_not_the_other_two(self, gate_tables, fake_runtime):
        """The boot secret carries three credentials. Reading the hook or gateway token would
        call the live bearer dead on every single evaluation."""
        old_hash, new_hash = self._arrange(gate_tables, fake_runtime, presents=self.NEW_PLAINTEXT)
        fake_runtime.secret_string = json.dumps(
            {"hookToken": self.NEW_PLAINTEXT, "gatewayToken": self.NEW_PLAINTEXT, "bedrockBearer": self.OLD_PLAINTEXT}
        )

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {old_hash, new_hash}


class TestDeny:
    @pytest.mark.asyncio
    async def test_toggles_denied_flag_on_the_current_token_row(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")
        token = await mod.mint_gate_token("org_abc", row_id="c-1")
        h = mod.hash_token(token)

        await mod.deny_gate_token("org_abc", True)
        row = gate_tables.get_item(TableName="test-gate-tokens", Key={"token_hash": {"S": h}})["Item"]
        assert row["denied"]["BOOL"] is True

        await mod.deny_gate_token("org_abc", False)
        row = gate_tables.get_item(TableName="test-gate-tokens", Key={"token_hash": {"S": h}})["Item"]
        assert row["denied"]["BOOL"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("denied_slot", ["current", "superseded"])
    async def test_a_denied_owners_new_derived_row_is_born_denied(self, gate_tables, fake_runtime, denied_slot):
        """A capped/killed owner's reconcile mints the derived bearer: its NEW row must not
        start at denied=False, or the rotation itself lifts the kill."""
        current, old = mod.hash_token("token-B"), mod.hash_token("token-A")
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=current, superseded=[old])
        _put_token_row(gate_tables, current)
        _put_token_row(gate_tables, old)
        gate_tables.update_item(
            TableName="test-gate-tokens",
            Key={"token_hash": {"S": current if denied_slot == "current" else old}},
            UpdateExpression="SET denied = :t",
            ExpressionAttributeValues={":t": {"BOOL": True}},
        )
        fake_runtime.bearer = "token-A"

        token = await mod.mint_gate_token("org_abc", row_id="c-1")

        assert _token_row(gate_tables, mod.hash_token(token))["denied"]["BOOL"] is True

    @pytest.mark.asyncio
    async def test_an_undenied_owners_new_row_is_not_denied(self, gate_tables, fake_runtime):
        current = mod.hash_token("token-B")
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=current)
        _put_token_row(gate_tables, current)
        fake_runtime.bearer = "token-B"

        token = await mod.mint_gate_token("org_abc", row_id="c-1")

        assert _token_row(gate_tables, mod.hash_token(token))["denied"]["BOOL"] is False

    @pytest.mark.asyncio
    async def test_denies_the_superseded_rows_too(self, gate_tables):
        """IMPORTANT 2. THE INCIDENT'S OWN SHAPE: mint B, the roll never lands,
        the service's task definition still carries A — so the container does
        ALL of its inference with A while the containers row points at B.

        Denying only the rotation pointer would flip a row nothing presents and
        leave the live credential untouched. Under the TTL this branch replaced,
        A died within an hour anyway; this design keeps it alive indefinitely
        precisely BECAUSE it is presentable, so the kill switch has to follow."""
        live_hash = mod.hash_token("token-A")  # what the container actually holds
        pointer_hash = mod.hash_token("token-B")  # the undelivered new token
        _put_container_row(
            gate_tables,
            "org_abc",
            bedrock_gate_token_hash=pointer_hash,
            superseded=[live_hash],
        )
        _put_token_row(gate_tables, live_hash)
        _put_token_row(gate_tables, pointer_hash)

        await mod.deny_gate_token("org_abc", True)

        denied = {r["token_hash"]["S"]: r["denied"]["BOOL"] for r in _token_rows(gate_tables)}
        assert denied == {live_hash: True, pointer_hash: True}

        # And the restore is symmetric — an un-revoke must not leave the token
        # the container is holding permanently dead.
        await mod.deny_gate_token("org_abc", False)
        restored = {r["token_hash"]["S"]: r["denied"]["BOOL"] for r in _token_rows(gate_tables)}
        assert restored == {live_hash: False, pointer_hash: False}

    @pytest.mark.asyncio
    async def test_a_tracked_hash_whose_row_is_gone_is_not_resurrected(self, gate_tables):
        """A tracked hash can outlive its row (deleted by an expiry that ran
        before the containers row was next written). The write is conditioned on
        the row existing, so the kill must not put a partial ``denied`` row back
        — and a restore must not recreate one with ``denied=false``."""
        pointer_hash = mod.hash_token("token-B")
        ghost = "9" * 64
        _put_container_row(
            gate_tables,
            "org_abc",
            bedrock_gate_token_hash=pointer_hash,
            superseded=[ghost],
        )
        _put_token_row(gate_tables, pointer_hash)

        await mod.deny_gate_token("org_abc", True)

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {pointer_hash}

    @pytest.mark.asyncio
    async def test_noop_when_owner_has_no_token_hash(self, gate_tables):
        # Owner row exists but carries no bedrock_gate_token_hash.
        _put_container_row(gate_tables, "org_none")
        await mod.deny_gate_token("org_none", True)  # must not raise
        assert _token_rows(gate_tables) == []

    @pytest.mark.asyncio
    async def test_noop_when_owner_row_absent(self, gate_tables):
        await mod.deny_gate_token("ghost", True)  # must not raise
        assert _token_rows(gate_tables) == []


class TestTableName:
    def test_override_env_wins(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_GATE_TOKENS_TABLE", "custom-tokens")
        assert mod._tokens_table_name() == "custom-tokens"

    def test_constructs_name_from_the_shared_table_prefix(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_GATE_TOKENS_TABLE", raising=False)
        monkeypatch.setattr(mod.settings, "DYNAMODB_TABLE_PREFIX", "lightbulb-prod-", raising=False)
        assert mod._tokens_table_name() == "lightbulb-prod-bedrock-gate-tokens"

    def test_environment_no_longer_participates(self, monkeypatch):
        """ENVIRONMENT used to build this name directly, which let it drift from
        the prefix CDK actually deploys. It must not influence the name now."""
        monkeypatch.delenv("BEDROCK_GATE_TOKENS_TABLE", raising=False)
        monkeypatch.setattr(mod.settings, "DYNAMODB_TABLE_PREFIX", "lightbulb-dev-", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod", raising=False)
        assert mod._tokens_table_name() == "lightbulb-dev-bedrock-gate-tokens"


def test_tokens_table_name_env_override_still_wins(monkeypatch):
    from core.billing import bedrock_gate_tokens

    monkeypatch.setenv("BEDROCK_GATE_TOKENS_TABLE", "explicit-table")
    assert bedrock_gate_tokens._tokens_table_name() == "explicit-table"


class TestAgentCoreDrain:
    """Task 21: for an AgentCore owner the drain reads ``GetAgentRuntime`` instead of the
    ECS task-definition/task enumeration (report 04 risk 3) -- one runtime per workspace,
    so one call and no per-task enumeration.

    The runtime's ``environmentVariables`` name the boot secret (``LIGHTBULB_SECRET_ID``),
    not the bearer plaintext -- there is exactly ONE boot-secret mechanism, M1's, already
    shipped in ``apps/infra/hermes/bridge/__init__.py`` / ``bridge/sync.load_boot_secrets``:
    one JSON secret ``lightbulb/{env}/hermes/{owner_id}/boot`` =
    ``{"hookToken","gatewayToken","bedrockBearer"}`` (M4-R4/R5 ruling). So the live bearer
    is resolved by fetching that secret and reading its ``bedrockBearer`` key.
    """

    @staticmethod
    def _row(overrides: dict):
        async def fake(_owner_id):
            return overrides

        return fake

    @staticmethod
    def _explodes_if_called(*_args, **_kwargs):
        raise AssertionError("must not be reached on this branch")

    @pytest.mark.asyncio
    async def test_an_agentcore_owner_presents_the_bearer_named_by_its_boot_secret(self, monkeypatch):
        monkeypatch.setattr(
            mod,
            "_container_row",
            self._row({"agentcore_runtime_arn": "arn:aws:x:1:runtime/r1"}),
        )
        monkeypatch.setattr(
            mod,
            "_describe_agent_runtime",
            lambda arn: {"environmentVariables": {"LIGHTBULB_SECRET_ID": "lightbulb/dev/hermes/org_a/boot"}},
        )
        monkeypatch.setattr(
            mod,
            "_secret_value",
            lambda name: json.dumps({"hookToken": "h", "gatewayToken": "g", "bedrockBearer": "live-token"}),
        )

        presentable = await mod._presentable_gate_token_hashes("org_a")

        assert presentable == {
            mod.hash_token("live-token"): "agentcore runtime r1 boot secret lightbulb/dev/hermes/org_a/boot"
        }

    @pytest.mark.asyncio
    async def test_a_runtime_naming_no_boot_secret_presents_nothing(self, monkeypatch):
        """A POSITIVE finding -- an empty answer, not an unknown one -- so the caller may
        delete. The runtime exists and names no secret, so no session from it holds a bearer."""
        monkeypatch.setattr(mod, "_container_row", self._row({"agentcore_runtime_arn": "arn:r1"}))
        monkeypatch.setattr(mod, "_describe_agent_runtime", lambda arn: {"environmentVariables": {}})
        monkeypatch.setattr(mod, "_secret_value", self._explodes_if_called)

        assert await mod._presentable_gate_token_hashes("org_a") == {}

    @pytest.mark.asyncio
    async def test_an_unreadable_runtime_raises_so_the_caller_keeps_the_rows(self, monkeypatch):
        monkeypatch.setattr(mod, "_container_row", self._row({"agentcore_runtime_arn": "arn:r1"}))

        def boom(_arn):
            raise RuntimeError("throttled")

        monkeypatch.setattr(mod, "_describe_agent_runtime", boom)

        with pytest.raises(RuntimeError):
            await mod._presentable_gate_token_hashes("org_a")

    @pytest.mark.asyncio
    async def test_a_malformed_boot_secret_raises_rather_than_reading_as_no_bearer(self, monkeypatch):
        """The convention is a fixed {hookToken, gatewayToken, bedrockBearer} shape. Anything
        else is incomplete data, not proof the runtime holds no bearer -- see _INCOMPLETE_NOTE."""
        monkeypatch.setattr(mod, "_container_row", self._row({"agentcore_runtime_arn": "arn:r1"}))
        monkeypatch.setattr(
            mod,
            "_describe_agent_runtime",
            lambda arn: {"environmentVariables": {"LIGHTBULB_SECRET_ID": "lightbulb/dev/hermes/org_a/boot"}},
        )
        monkeypatch.setattr(mod, "_secret_value", lambda name: "not-json")

        with pytest.raises(RuntimeError):
            await mod._presentable_gate_token_hashes("org_a")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("secret_string", ["", None])
    async def test_an_empty_or_absent_secret_string_raises_rather_than_reading_as_no_bearer(
        self, monkeypatch, secret_string
    ):
        """A named boot secret with no body (empty ``SecretString``, or the key entirely
        absent from the GetSecretValue response) is the SAME incomplete-data class as
        malformed JSON -- the secret exists (``secret_id`` resolved), so an empty read is
        unknown, not a positive finding that the runtime holds no bearer."""
        monkeypatch.setattr(mod, "_container_row", self._row({"agentcore_runtime_arn": "arn:r1"}))
        monkeypatch.setattr(
            mod,
            "_describe_agent_runtime",
            lambda arn: {"environmentVariables": {"LIGHTBULB_SECRET_ID": "lightbulb/dev/hermes/org_a/boot"}},
        )
        monkeypatch.setattr(mod, "_secret_value", lambda name: secret_string)

        with pytest.raises(RuntimeError):
            await mod._presentable_gate_token_hashes("org_a")
