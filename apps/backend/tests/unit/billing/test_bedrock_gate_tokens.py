"""Unit tests for core.billing.bedrock_gate_tokens — per-owner gate bearer tokens.

Covers the load-bearing invariants: hash-only storage (no plaintext ever
persisted), the {tier: arn} -> {arn: tier} aip_tiers inversion the gateway keys
on, the liveness predicate that decides when a superseded token row may finally
be deleted, and the deny kill-switch toggle.
"""

import hashlib

import boto3
import pytest
from moto import mock_aws

from core.billing import bedrock_gate_tokens as mod

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


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
    }
    for k, v in attrs.items():
        item[k] = {"S": v}
    if superseded is not None:
        item[mod._SUPERSEDED_FIELD] = {"L": [{"S": h} for h in superseded]}
    client.put_item(TableName="test-containers", Item=item)


def _token_rows(client) -> list[dict]:
    return client.scan(TableName="test-gate-tokens")["Items"]


def _put_token_row(client, token_hash: str, owner_id: str = "org_abc") -> None:
    client.put_item(
        TableName="test-gate-tokens",
        Item={
            "token_hash": {"S": token_hash},
            "owner_id": {"S": owner_id},
            "denied": {"BOOL": False},
        },
    )


def _container_row(client, owner_id: str = "org_abc") -> dict:
    return client.get_item(TableName="test-containers", Key={"owner_id": {"S": owner_id}})["Item"]


def _tracked(client, owner_id: str = "org_abc") -> list[str]:
    row = _container_row(client, owner_id)
    return [v["S"] for v in row.get(mod._SUPERSEDED_FIELD, {}).get("L", [])]


# ---------------------------------------------------------------------------
# A fake ECS control plane. The predicate's whole job is to read ECS state, so
# the scenarios below are expressed as ECS state and the assertions are on the
# real DynamoDB rows that survive.
# ---------------------------------------------------------------------------

_OLD_DEF = "arn:aws:ecs:us-east-1:1:task-definition/isol8-prod-openclaw-user:125"
_NEW_DEF = "arn:aws:ecs:us-east-1:1:task-definition/isol8-prod-openclaw-user:126"


class FakeEcs:
    """Minimal stand-in for the four ECS calls the predicate makes."""

    def __init__(
        self,
        *,
        service_task_def: str | None = _OLD_DEF,
        service_status: str = "ACTIVE",
        service_missing: bool = False,
        tasks: list[dict] | None = None,
        task_def_tokens: dict[str, str | None] | None = None,
        raises: Exception | None = None,
    ):
        self.service_task_def = service_task_def
        self.service_status = service_status
        self.service_missing = service_missing
        # Each task: {"taskDefinitionArn", "desiredStatus", "lastStatus"}
        self.tasks = tasks or []
        self.task_def_tokens = task_def_tokens or {}
        self.raises = raises
        self.calls: list[str] = []
        # --- partial-response knobs. ECS reports per-resource problems INSIDE a
        # 200, so these are the shapes no `except` can see.
        self.service_failures: list[dict] | None = None  # overrides the MISSING failure
        self.list_tasks_page_size: int | None = None  # paginate ListTasks at this size
        self.describe_tasks_failures: list[dict] = []  # non-empty failures on DescribeTasks
        self.describe_tasks_drop: set[str] = set()  # ARNs silently omitted from `tasks`

    def _task_arn(self, index: int) -> str:
        return f"arn:aws:ecs:us-east-1:1:task/{index}"

    def describe_services(self, *, cluster, services):
        self.calls.append("describe_services")
        if self.raises:
            raise self.raises
        if self.service_failures is not None:
            return {"services": [], "failures": self.service_failures}
        if self.service_missing:
            return {"services": [], "failures": [{"arn": services[0], "reason": "MISSING"}]}
        svc = {"serviceName": services[0], "status": self.service_status}
        if self.service_task_def:
            svc["taskDefinition"] = self.service_task_def
        return {"services": [svc], "failures": []}

    def list_tasks(self, *, cluster, serviceName, desiredStatus, nextToken=None):
        self.calls.append(f"list_tasks:{desiredStatus}:{nextToken or 'p0'}")
        arns = [self._task_arn(i) for i, t in enumerate(self.tasks) if t["desiredStatus"] == desiredStatus]
        if not self.list_tasks_page_size:
            return {"taskArns": arns}
        # ECS returns at most maxResults ARNs plus a nextToken, in no order.
        offset = int(nextToken.split(":")[-1]) if nextToken else 0
        page = arns[offset : offset + self.list_tasks_page_size]
        resp = {"taskArns": page}
        if offset + self.list_tasks_page_size < len(arns):
            resp["nextToken"] = f"{desiredStatus}:{offset + self.list_tasks_page_size}"
        return resp

    def describe_tasks(self, *, cluster, tasks):
        self.calls.append("describe_tasks")
        wanted = set(tasks) - self.describe_tasks_drop
        return {
            "tasks": [
                {
                    "taskArn": self._task_arn(i),
                    "taskDefinitionArn": t["taskDefinitionArn"],
                    "lastStatus": t["lastStatus"],
                }
                for i, t in enumerate(self.tasks)
                if self._task_arn(i) in wanted
            ],
            "failures": list(self.describe_tasks_failures),
        }

    def describe_task_definition(self, *, taskDefinition):
        self.calls.append(f"describe_task_definition:{taskDefinition}")
        token = self.task_def_tokens.get(taskDefinition)
        env = [{"name": "AWS_REGION", "value": "us-east-1"}]
        if token:
            env.append({"name": "AWS_BEARER_TOKEN_BEDROCK", "value": token})
        return {"taskDefinition": {"containerDefinitions": [{"name": "openclaw", "environment": env}]}}


@pytest.fixture
def fake_ecs(monkeypatch):
    """Install a FakeEcs; the test mutates it before calling into the module."""
    ecs = FakeEcs()
    monkeypatch.setattr(mod, "_owner_service_view", lambda owner_id: (ecs, "cluster-arn", "openclaw-svc"))
    return ecs


class TestMint:
    @pytest.mark.asyncio
    async def test_logs_owner_and_both_hash_prefixes(self, gate_tables, fake_ecs, caplog):
        """The mint log line is the whole point of this module: it must carry
        enough to correlate against a `describe-task-definition` without
        logging anything a reader could turn back into a working credential."""
        old_hash = mod.hash_token("old-plaintext")
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        _put_token_row(gate_tables, old_hash)
        fake_ecs.task_def_tokens = {_OLD_DEF: "old-plaintext"}

        with caplog.at_level("INFO", logger=mod.logger.name):
            token = await mod.mint_gate_token("org_abc", _AIPS)

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
    async def test_logs_none_for_a_first_mint_with_no_prior_token(self, gate_tables, fake_ecs, caplog):
        _put_container_row(gate_tables, "org_abc")

        with caplog.at_level("INFO", logger=mod.logger.name):
            await mod.mint_gate_token("org_abc", _AIPS)

        mint_records = [r for r in caplog.records if "minted" in r.message]
        assert len(mint_records) == 1
        assert "prior=none" in mint_records[0].message

    @pytest.mark.asyncio
    async def test_no_captured_log_output_contains_the_plaintext_token(self, gate_tables, fake_ecs, caplog):
        """The constraint most likely to regress: assert it explicitly rather
        than trust review to catch a future `logger.debug(token)`."""
        old_hash = mod.hash_token("old-plaintext")
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        _put_token_row(gate_tables, old_hash)
        fake_ecs.task_def_tokens = {_OLD_DEF: "old-plaintext"}

        with caplog.at_level("DEBUG"):
            token = await mod.mint_gate_token("org_abc", _AIPS)
            await mod.expire_superseded_gate_tokens("org_abc")

        for record in caplog.records:
            assert token not in record.getMessage()
            assert "old-plaintext" not in record.getMessage()

    @pytest.mark.asyncio
    async def test_writes_hash_only_row_with_inverted_aip_tiers(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")

        token = await mod.mint_gate_token("org_abc", _AIPS)

        rows = _token_rows(gate_tables)
        assert len(rows) == 1
        row = rows[0]
        # PK is the SHA-256 hex of the returned plaintext.
        assert row["token_hash"]["S"] == hashlib.sha256(token.encode()).hexdigest()
        assert row["owner_id"]["S"] == "org_abc"
        assert row["denied"]["BOOL"] is False
        assert "created_at" in row
        # aip_tiers is the INVERSE of the owner's {tier: arn} map: {arn: tier}.
        stored = {arn: v["S"] for arn, v in row["aip_tiers"]["M"].items()}
        assert stored == {arn: tier for tier, arn in _AIPS.items()}

    @pytest.mark.asyncio
    async def test_no_plaintext_token_is_persisted_anywhere(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")

        token = await mod.mint_gate_token("org_abc", _AIPS)

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

        token = await mod.mint_gate_token("org_abc", _AIPS)

        crow = gate_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert crow["bedrock_gate_token_hash"]["S"] == mod.hash_token(token)

    @pytest.mark.asyncio
    async def test_minting_never_puts_the_prior_row_on_a_clock(self, gate_tables, fake_ecs):
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
        fake_ecs.task_def_tokens = {_OLD_DEF: "old-plaintext"}

        token = await mod.mint_gate_token("org_abc", _AIPS)

        rows = {r["token_hash"]["S"]: r for r in _token_rows(gate_tables)}
        assert old_hash in rows and mod.hash_token(token) in rows
        # No row carries a ttl — nothing is on a fuse anymore.
        assert all("ttl" not in r for r in rows.values())
        # The superseded hash is TRACKED instead, for the predicate to judge later.
        assert _tracked(gate_tables) == [old_hash]

    @pytest.mark.asyncio
    async def test_mint_backstop_drains_an_orphaned_hash_but_keeps_the_live_one(self, gate_tables, fake_ecs):
        """Mint is the backstop for a confirmation that never came.

        Two supersessions with no roll in between: the FIRST one's token is not
        in any task definition (it was never delivered) and must go; the one the
        service still references must survive.
        """
        orphan = "1" * 64
        live_plaintext = "live-token"
        live_hash = mod.hash_token(live_plaintext)
        _put_container_row(
            gate_tables,
            "org_abc",
            bedrock_gate_token_hash=live_hash,
            superseded=[orphan],
        )
        _put_token_row(gate_tables, orphan)
        _put_token_row(gate_tables, live_hash)
        fake_ecs.task_def_tokens = {_OLD_DEF: live_plaintext}

        token = await mod.mint_gate_token("org_abc", _AIPS)

        remaining = {r["token_hash"]["S"] for r in _token_rows(gate_tables)}
        assert orphan not in remaining
        assert remaining == {live_hash, mod.hash_token(token)}
        assert _tracked(gate_tables) == [live_hash]

    @pytest.mark.asyncio
    async def test_returned_tokens_are_unique_per_mint(self, gate_tables, fake_ecs):
        _put_container_row(gate_tables, "org_abc")
        t1 = await mod.mint_gate_token("org_abc", _AIPS)
        t2 = await mod.mint_gate_token("org_abc", _AIPS)
        assert t1 != t2
        # The superseded token OUTLIVES the mint by design — it is tracked, not
        # scheduled — and only goes when the predicate proves it unreachable.
        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {
            mod.hash_token(t1),
            mod.hash_token(t2),
        }
        assert _tracked(gate_tables) == [mod.hash_token(t1)]

        fake_ecs.service_missing = True  # owner has no service: nothing holds t1
        await mod.expire_superseded_gate_tokens("org_abc")

        assert [r["token_hash"]["S"] for r in _token_rows(gate_tables)] == [mod.hash_token(t2)]


class TestConcurrentMints:
    """Two mints for the same owner, both reading before either writes.

    Reachable in production: the AIP/role/token mint block in
    ``provision_user_container`` sits AHEAD of the first
    ``_provision_checkpoint``, so the provision lease does not serialize it —
    a poll provision and an escalating ``container_recover`` takeover can both
    be inside it at once.
    """

    @staticmethod
    def _interleave(monkeypatch, owner_id: str = "org_abc") -> dict:
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
                state["token_b"] = await mod.mint_gate_token(owner_id, _AIPS)
            return await real_swap(*args, **kwargs)

        monkeypatch.setattr(container_repo, "swap_gate_token_hash", swap)
        return state

    @pytest.mark.asyncio
    async def test_both_minted_hashes_stay_reachable(self, gate_tables, fake_ecs, monkeypatch):
        """Neither mint may erase the other's hash from the row.

        The row is the ONLY handle on a gate-token row (hash-keyed table, no
        owner index), so a hash dropped here is a denied=False credential that
        can never be expired or denied again.
        """
        _put_container_row(gate_tables, "org_abc")
        state = self._interleave(monkeypatch)

        token_a = await mod.mint_gate_token("org_abc", _AIPS)

        hash_a, hash_b = mod.hash_token(token_a), mod.hash_token(state["token_b"])
        assert hash_a != hash_b
        row = _container_row(gate_tables)
        reachable = {row["bedrock_gate_token_hash"]["S"], *_tracked(gate_tables)}
        assert reachable == {hash_a, hash_b}
        # Both token rows still exist — the loser was tracked, not orphaned.
        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {hash_a, hash_b}

    @pytest.mark.asyncio
    async def test_deny_reaches_the_race_loser(self, gate_tables, fake_ecs, monkeypatch):
        """THE CONSEQUENCE. An untracked hash is a live bearer credential the
        trial-cap backstop and admin revoke cannot kill."""
        _put_container_row(gate_tables, "org_abc")
        state = self._interleave(monkeypatch)

        token_a = await mod.mint_gate_token("org_abc", _AIPS)

        await mod.deny_gate_token("org_abc", True)

        denied = {r["token_hash"]["S"]: r["denied"]["BOOL"] for r in _token_rows(gate_tables)}
        assert denied == {mod.hash_token(token_a): True, mod.hash_token(state["token_b"]): True}

    @pytest.mark.asyncio
    async def test_retry_exhaustion_logs_and_does_not_raise_or_re_mint(
        self, gate_tables, fake_ecs, monkeypatch, caplog
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
            token = await mod.mint_gate_token("org_abc", _AIPS)

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
    async def test_a_conflict_is_never_misread_as_absent_by_a_stale_replica(self, gate_tables, fake_ecs, monkeypatch):
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

        token = await mod.mint_gate_token("org_abc", _AIPS)

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
    async def test_a_later_fold_in_upsert_cannot_drop_a_swapped_hash(self, gate_tables, fake_ecs):
        """The composition, one function later. The conditional swap is not the
        last write to the pointer: create_user_service folds the caller's own
        minted hash into a whole-item upsert.

        Shape: a container_recover takeover mints HB and swaps; the superseded
        poll provision mints HA and swaps (correctly merging HB); it then dies
        at its next _provision_checkpoint, and the survivor's fold-in writes HB
        back over the top. HA must survive that as a tracked hash.
        """
        h0_plaintext = "h0-token"
        h0 = mod.hash_token(h0_plaintext)
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=h0)
        _put_token_row(gate_tables, h0)
        fake_ecs.task_def_tokens = {_OLD_DEF: h0_plaintext}  # H0 is live: the drain must keep it

        token_b = await mod.mint_gate_token("org_abc", _AIPS)
        token_a = await mod.mint_gate_token("org_abc", _AIPS)
        assert _tracked(gate_tables) == [h0, mod.hash_token(token_b)]

        # The survivor's create_user_service fold-in.
        from core.repositories import container_repo

        await container_repo.upsert(
            "org_abc",
            {"status": "provisioning", "bedrock_gate_token_hash": mod.hash_token(token_b)},
        )

        row = _container_row(gate_tables)
        reachable = {row["bedrock_gate_token_hash"]["S"], *_tracked(gate_tables)}
        assert reachable == {h0, mod.hash_token(token_a), mod.hash_token(token_b)}

        await mod.deny_gate_token("org_abc", True)
        assert {r["token_hash"]["S"]: r["denied"]["BOOL"] for r in _token_rows(gate_tables)} == {
            h0: True,
            mod.hash_token(token_a): True,
            mod.hash_token(token_b): True,
        }

    @pytest.mark.asyncio
    async def test_absent_row_stays_a_silent_no_op(self, gate_tables, fake_ecs, caplog):
        """The fresh-provision path: no containers row yet. The swap must not
        create one (create_user_service's upsert folds the hash in) and must not
        look like a lost race."""
        with caplog.at_level("ERROR", logger=mod.logger.name):
            token = await mod.mint_gate_token("org_new", _AIPS)

        assert [r["token_hash"]["S"] for r in _token_rows(gate_tables)] == [mod.hash_token(token)]
        assert "Item" not in gate_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_new"}})
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []


class TestExpireSuperseded:
    """The liveness predicate: a token row lives until nothing can present it.

    The container gets its token ONLY as an inline env var in an ECS task
    definition, frozen at task start, so the authoritative question is what the
    SERVICE's task definition carries — what will launch next. Not-yet-stopped
    tasks are the second half; at the points this is actually evaluated they
    only ever re-contribute the service's own definition, so that half decides
    nothing today. Every case here is a scenario per-user containers produce.
    """

    OLD_PLAINTEXT = "token-A"
    NEW_PLAINTEXT = "token-B"

    def _arrange(self, client, fake_ecs, *, service_def, tasks=None):
        """Owner mid-rotation: current token B, superseded token A still tracked."""
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
        fake_ecs.service_task_def = service_def
        fake_ecs.task_def_tokens = {_OLD_DEF: self.OLD_PLAINTEXT, _NEW_DEF: self.NEW_PLAINTEXT}
        if tasks is not None:
            fake_ecs.tasks = tasks
        return old_hash, new_hash

    @pytest.mark.asyncio
    async def test_normal_reprovision_expires_the_superseded_token(self, gate_tables, fake_ecs):
        """Service is on the NEW def and the old task is gone — nothing holds A."""
        old_hash, new_hash = self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_NEW_DEF,
            tasks=[{"taskDefinitionArn": _NEW_DEF, "desiredStatus": "RUNNING", "lastStatus": "RUNNING"}],
        )

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {new_hash}

    @pytest.mark.asyncio
    async def test_the_incident_service_still_references_the_old_def(self, gate_tables, fake_ecs):
        """THE INCIDENT. The roll never landed: the service still points at the
        def carrying token A, so the container that launches from it presents A.
        Expiring A here is what killed a paying customer's container for good."""
        old_hash, new_hash = self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_OLD_DEF,
            # 16:57:59 in the incident: the service was DRAINED, zero tasks
            # running. "Expire when no task holds it" would delete A right here.
            tasks=[],
        )

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_scale_to_zero_keeps_the_token(self, gate_tables, fake_ecs):
        """Idle trial container parked at desiredCount=0. The service still
        references its def, so its token must survive an idle week."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF, tasks=[])

        await mod.expire_superseded_gate_tokens("org_abc")

        assert old_hash in {r["token_hash"]["S"] for r in _token_rows(gate_tables)}

    @pytest.mark.asyncio
    async def test_crash_restart_on_the_same_task_def_keeps_the_token(self, gate_tables, fake_ecs):
        """OOM/health restart: ECS relaunches from the SAME def, so the
        replacement task presents the same token."""
        old_hash, _ = self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_OLD_DEF,
            tasks=[{"taskDefinitionArn": _OLD_DEF, "desiredStatus": "RUNNING", "lastStatus": "PENDING"}],
        )

        await mod.expire_superseded_gate_tokens("org_abc")

        assert old_hash in {r["token_hash"]["S"] for r in _token_rows(gate_tables)}

    @pytest.mark.asyncio
    async def test_overlapping_deployment_keeps_the_token_while_the_old_task_drains(self, gate_tables, fake_ecs):
        """An old task on its way out (desiredStatus=STOPPED) while the new one
        runs. SINGLE_WRITER_DEPLOYMENT_CONFIG bars the two from RUNNING
        together, but not from being LISTED together — a STOPPING/DEPROVISIONING
        task has desiredStatus=STOPPED by definition and is not counted by
        runningCount, so ECS can start the replacement while it tears down. What
        makes the branch inert is that the roll confirmation fires after the old
        task is gone. Pinned so that raising maximumPercent (for a zero-downtime
        deploy, say), or evaluating mid-teardown, does not silently start
        expiring the token of a container that is still serving."""
        old_hash, new_hash = self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_NEW_DEF,
            tasks=[
                {"taskDefinitionArn": _OLD_DEF, "desiredStatus": "STOPPED", "lastStatus": "RUNNING"},
                {"taskDefinitionArn": _NEW_DEF, "desiredStatus": "RUNNING", "lastStatus": "RUNNING"},
            ],
        )

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_redeploy_expires_once_the_old_task_has_stopped(self, gate_tables, fake_ecs):
        """What a real per-user redeploy looks like: single-writer stops the old
        task before the new one starts, so by the time the new task is RUNNING
        nothing can present A."""
        old_hash, new_hash = self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_NEW_DEF,
            tasks=[
                {"taskDefinitionArn": _OLD_DEF, "desiredStatus": "STOPPED", "lastStatus": "STOPPED"},
                {"taskDefinitionArn": _NEW_DEF, "desiredStatus": "RUNNING", "lastStatus": "RUNNING"},
            ],
        )

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {new_hash}

    @pytest.mark.asyncio
    async def test_deleted_owner_expires_the_token(self, gate_tables, fake_ecs):
        """No service at all: nothing will ever launch with it."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=None)
        fake_ecs.service_missing = True

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {new_hash}

    @pytest.mark.asyncio
    async def test_inactive_service_expires_the_token(self, gate_tables, fake_ecs):
        """A deleted service lingers as INACTIVE for a while; it launches nothing."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF)
        fake_ecs.service_status = "INACTIVE"

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {new_hash}

    @pytest.mark.asyncio
    async def test_ecs_failure_keeps_the_token_and_does_not_raise(self, gate_tables, fake_ecs):
        """Fail toward availability: an unprovable token is a LIVE token."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF)
        fake_ecs.raises = RuntimeError("ThrottlingException: Rate exceeded")

        await mod.expire_superseded_gate_tokens("org_abc")  # must not raise

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_a_dynamodb_failure_does_not_raise(self, gate_tables, fake_ecs, monkeypatch):
        self._arrange(gate_tables, fake_ecs, service_def=_NEW_DEF)

        async def boom(*_a, **_kw):
            raise RuntimeError("ProvisionedThroughputExceeded")

        monkeypatch.setattr(mod.container_repo, "get_by_owner_id", boom)

        await mod.expire_superseded_gate_tokens("org_abc")  # must not raise

    @pytest.mark.asyncio
    async def test_never_deletes_the_owners_current_token(self, gate_tables, fake_ecs):
        """Defensive: if the CURRENT hash ever lands in the tracked list, it is
        not superseded and must never be a deletion candidate — even though no
        task definition carries it yet (a fresh mint before the roll)."""
        current = mod.hash_token(self.NEW_PLAINTEXT)
        _put_container_row(
            gate_tables,
            "org_abc",
            bedrock_gate_token_hash=current,
            superseded=[current],
        )
        _put_token_row(gate_tables, current)
        fake_ecs.service_task_def = _OLD_DEF
        fake_ecs.task_def_tokens = {_OLD_DEF: self.OLD_PLAINTEXT}

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {current}
        assert fake_ecs.calls == []  # not even evaluated

    @pytest.mark.asyncio
    async def test_non_bedrock_owner_is_never_evaluated(self, gate_tables, fake_ecs):
        """chatgpt_oauth / byo_key owners never mint a gate token, so they carry
        no hash and no tracked list — the path must stop before touching ECS."""
        _put_container_row(gate_tables, "org_oauth", provider_choice="chatgpt_oauth")

        await mod.expire_superseded_gate_tokens("org_oauth")

        assert fake_ecs.calls == []
        assert _token_rows(gate_tables) == []

    @pytest.mark.asyncio
    async def test_missing_container_row_is_a_noop(self, gate_tables, fake_ecs):
        await mod.expire_superseded_gate_tokens("ghost")
        assert fake_ecs.calls == []

    @pytest.mark.asyncio
    async def test_kept_verdict_logs_which_half_of_the_predicate_held_it(self, gate_tables, fake_ecs, caplog):
        """THE INCIDENT scenario again, but asserting the log line this time:
        a bare 'kept' would have left the same blind spot the incident did."""
        old_hash, _ = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF, tasks=[])

        with caplog.at_level("INFO", logger=mod.logger.name):
            await mod.expire_superseded_gate_tokens("org_abc")

        kept = [r.message for r in caplog.records if "kept" in r.message]
        assert len(kept) == 1
        assert "org_abc" in kept[0]
        assert old_hash[: mod._HASH_PREFIX_LEN] in kept[0]
        assert old_hash not in kept[0]
        assert "service task definition still references it" in kept[0]

    @pytest.mark.asyncio
    async def test_kept_verdict_names_the_unstopped_task_reason(self, gate_tables, fake_ecs, caplog):
        """The other reason string. It must NOT claim the task is "still
        running": the live half admits DEACTIVATING/STOPPING/DEPROVISIONING, and
        at mint time (seconds after the service is stopped in the conversion
        flow) the outgoing task is routinely mid-teardown."""
        old_hash, _ = self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_NEW_DEF,
            tasks=[
                # DEACTIVATING, not RUNNING: this is what the branch really sees.
                {"taskDefinitionArn": _OLD_DEF, "desiredStatus": "STOPPED", "lastStatus": "DEACTIVATING"},
                {"taskDefinitionArn": _NEW_DEF, "desiredStatus": "RUNNING", "lastStatus": "RUNNING"},
            ],
        )

        with caplog.at_level("INFO", logger=mod.logger.name):
            await mod.expire_superseded_gate_tokens("org_abc")

        kept = [
            r.message for r in caplog.records if "kept" in r.message and old_hash[: mod._HASH_PREFIX_LEN] in r.message
        ]
        assert len(kept) == 1
        assert "a task launched from it has not stopped yet" in kept[0]
        # Must not assert "running" about a task that is shutting down.
        assert "still running" not in kept[0]

    @pytest.mark.asyncio
    async def test_expired_verdict_logs_its_reason(self, gate_tables, fake_ecs, caplog):
        old_hash, _ = self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_NEW_DEF,
            tasks=[{"taskDefinitionArn": _NEW_DEF, "desiredStatus": "RUNNING", "lastStatus": "RUNNING"}],
        )

        with caplog.at_level("INFO", logger=mod.logger.name):
            await mod.expire_superseded_gate_tokens("org_abc")

        expired = [r.message for r in caplog.records if "expired" in r.message]
        assert len(expired) == 1
        assert "org_abc" in expired[0]
        assert old_hash[: mod._HASH_PREFIX_LEN] in expired[0]
        assert old_hash not in expired[0]
        assert "no task definition or unstopped task presents it" in expired[0]

    @pytest.mark.asyncio
    async def test_expiry_logs_once_not_on_every_later_evaluation(self, gate_tables, fake_ecs, caplog):
        """MINOR 4. The survivor list is deliberately not written back, so the
        hash stays tracked until the next mint and every later wake re-runs the
        predicate and re-issues the delete. Only the evaluation that actually
        REMOVED the row may log 'expired' — otherwise N re-confirmations are
        indistinguishable from N expiries in the logs."""
        self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_NEW_DEF,
            tasks=[{"taskDefinitionArn": _NEW_DEF, "desiredStatus": "RUNNING", "lastStatus": "RUNNING"}],
        )

        with caplog.at_level("INFO", logger=mod.logger.name):
            await mod.expire_superseded_gate_tokens("org_abc")
            await mod.expire_superseded_gate_tokens("org_abc")
            await mod.expire_superseded_gate_tokens("org_abc")

        assert len([r for r in caplog.records if "expired" in r.message]) == 1

    @pytest.mark.asyncio
    async def test_a_live_service_with_no_task_definition_keeps_the_rows(self, gate_tables, fake_ecs):
        """MINOR 3. The one branch where missing data used to mean DELETE.

        DescribeServices on a live service always carries taskDefinition, so its
        absence is incomplete information, not 'this service launches nothing'.
        Every other unknown here fails safe (ECS error -> keep; unrecognised
        service status -> treated as live), and this one must too: the container
        may well be running on a definition we simply failed to read."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=None)
        fake_ecs.service_missing = False  # the service EXISTS; only the field is absent

        await mod.expire_superseded_gate_tokens("org_abc")  # must not raise

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_reads_the_bedrock_bearer_env_not_the_acp_harness_one(self, gate_tables, fake_ecs, monkeypatch):
        """The same token is injected twice, but ANTHROPIC_AUTH_TOKEN is
        PAID-ONLY. A free-tier task def carries only AWS_BEARER_TOKEN_BEDROCK;
        reading the harness var would call its live token dead."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF)

        def trial_task_def(*, taskDefinition):
            return {
                "taskDefinition": {
                    "containerDefinitions": [
                        {
                            "name": "openclaw",
                            "environment": [
                                {"name": "AWS_BEARER_TOKEN_BEDROCK", "value": self.OLD_PLAINTEXT},
                            ],
                        }
                    ]
                }
            }

        monkeypatch.setattr(fake_ecs, "describe_task_definition", trial_task_def)

        await mod.expire_superseded_gate_tokens("org_abc")

        assert old_hash in {r["token_hash"]["S"] for r in _token_rows(gate_tables)}


class TestPartialEcsResponses:
    """ECS reports per-resource failures INSIDE a successful response.

    So the module's fail-open posture — a broad ``except`` around the predicate
    — does not cover any of these: nothing raises, the code just gets less data
    than it asked for. Read as complete, every shape below resolves to "nothing
    can present this token" and PERMANENTLY deletes a credential the running
    container is using; its next inference call dies with
    ``UnrecognizedClientException``. That is the lockout direction the module is
    not allowed to fail in, so incomplete must mean unknown must mean keep.
    """

    def _arrange(self, client, fake_ecs, **kwargs) -> tuple[str, str]:
        """Same owner-mid-rotation fixture the expiry suite uses — including its
        plaintexts, so there is nothing to keep in sync here."""
        return TestExpireSuperseded()._arrange(client, fake_ecs, **kwargs)

    def _hashes(self, client) -> set[str]:
        return {r["token_hash"]["S"] for r in _token_rows(client)}

    @pytest.mark.asyncio
    async def test_a_service_failure_that_is_not_missing_keeps_the_rows(self, gate_tables, fake_ecs):
        """Only MISSING proves absence. Any other reason is a fault reported in
        the response body — throttling, an access denial, a cluster we cannot
        read — and says nothing about whether the service exists."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF)
        fake_ecs.service_failures = [{"arn": "openclaw-svc", "reason": "ACCESS_DENIED"}]

        await mod.expire_superseded_gate_tokens("org_abc")  # must not raise

        assert self._hashes(gate_tables) == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_a_response_naming_neither_our_service_nor_a_failure_keeps_the_rows(self, gate_tables, fake_ecs):
        """Neither described nor reported missing: we simply do not know."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF)
        fake_ecs.service_failures = []  # empty services AND empty failures

        await mod.expire_superseded_gate_tokens("org_abc")

        assert self._hashes(gate_tables) == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_a_failure_about_a_different_service_keeps_the_rows(self, gate_tables, fake_ecs):
        """A MISSING failure is only proof when it is about the service we
        asked for."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF)
        fake_ecs.service_failures = [{"arn": "openclaw-somebody-else", "reason": "MISSING"}]

        await mod.expire_superseded_gate_tokens("org_abc")

        assert self._hashes(gate_tables) == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_positive_absence_still_expires(self, gate_tables, fake_ecs):
        """THE GUARD ON THE GUARD. Tightening absence must not stop the genuine
        deleted-owner path from expiring — an explicit MISSING for our ARN, and
        an INACTIVE service, are both positive proof."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF)
        fake_ecs.service_failures = [
            {"arn": "arn:aws:ecs:us-east-1:1:service/cluster/openclaw-svc", "reason": "MISSING"}
        ]

        await mod.expire_superseded_gate_tokens("org_abc")

        assert self._hashes(gate_tables) == {new_hash}

        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF)
        fake_ecs.service_failures = None
        fake_ecs.service_status = "INACTIVE"

        await mod.expire_superseded_gate_tokens("org_abc")

        assert self._hashes(gate_tables) == {new_hash}

    @pytest.mark.asyncio
    async def test_list_tasks_is_paginated_before_any_deletion_decision(self, gate_tables, fake_ecs):
        """ListTasks returns <=100 ARNs plus a nextToken, in NO guaranteed
        order. A crash-looping service leaves a long stopped-task history, so
        the one still-terminating task on the SUPERSEDED definition can sit on
        page 2 while page 1 is all new-definition churn. Reading page 1 as the
        whole answer deletes the token that task is presenting."""
        # Service already rolled to the new def; the old token is held only by a
        # task that has not finished stopping — and it is listed last.
        tasks = [
            {"taskDefinitionArn": _NEW_DEF, "desiredStatus": "STOPPED", "lastStatus": "STOPPED"},
            {"taskDefinitionArn": _NEW_DEF, "desiredStatus": "STOPPED", "lastStatus": "STOPPED"},
            {"taskDefinitionArn": _OLD_DEF, "desiredStatus": "STOPPED", "lastStatus": "DEPROVISIONING"},
        ]
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_NEW_DEF, tasks=tasks)
        fake_ecs.list_tasks_page_size = 2

        await mod.expire_superseded_gate_tokens("org_abc")

        assert self._hashes(gate_tables) == {old_hash, new_hash}
        # Every page consumed, not just the first.
        assert [c for c in fake_ecs.calls if c.startswith("list_tasks:STOPPED")] == [
            "list_tasks:STOPPED:p0",
            "list_tasks:STOPPED:STOPPED:2",
        ]

    @pytest.mark.asyncio
    async def test_describe_tasks_failures_keep_the_rows(self, gate_tables, fake_ecs):
        old_hash, new_hash = self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_NEW_DEF,
            tasks=[{"taskDefinitionArn": _NEW_DEF, "desiredStatus": "RUNNING", "lastStatus": "RUNNING"}],
        )
        fake_ecs.describe_tasks_failures = [{"arn": "arn:aws:ecs:us-east-1:1:task/9", "reason": "MISSING"}]

        await mod.expire_superseded_gate_tokens("org_abc")

        assert self._hashes(gate_tables) == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_a_silently_dropped_task_keeps_the_rows(self, gate_tables, fake_ecs):
        """The worst shape: the omitted ARN was the unstopped task on the
        superseded definition, so the short list reads as 'nothing holds it'."""
        tasks = [
            {"taskDefinitionArn": _NEW_DEF, "desiredStatus": "RUNNING", "lastStatus": "RUNNING"},
            {"taskDefinitionArn": _OLD_DEF, "desiredStatus": "STOPPED", "lastStatus": "DEPROVISIONING"},
        ]
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_NEW_DEF, tasks=tasks)
        fake_ecs.describe_tasks_drop = {"arn:aws:ecs:us-east-1:1:task/1"}

        await mod.expire_superseded_gate_tokens("org_abc")

        assert self._hashes(gate_tables) == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_a_task_definition_with_no_container_definitions_keeps_the_rows(self, gate_tables, fake_ecs):
        """Every registered task definition has at least one container
        definition, so a reply without them is missing data — not 'this
        definition carries no gate token'."""
        old_hash, new_hash = self._arrange(gate_tables, fake_ecs, service_def=_OLD_DEF)
        fake_ecs.describe_task_definition = lambda *, taskDefinition: {"taskDefinition": {}}

        await mod.expire_superseded_gate_tokens("org_abc")

        assert self._hashes(gate_tables) == {old_hash, new_hash}


class TestOwnerServiceView:
    """MINOR 6. Every other test monkeypatches ``_owner_service_view`` whole, so
    the three ``EcsManager`` privates it reaches into go unexercised. A rename
    there raises AttributeError, which ``_drain_superseded`` swallows into "keep
    the row" — safe, but the expiry mechanism is then permanently dead behind a
    warning line. Run against a REAL EcsManager (boto3 stubbed) so the three
    names are bound to the class, not to a hand-written double."""

    def test_reads_the_ecs_client_cluster_and_derived_service_name(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from core.containers.ecs_manager import EcsManager

        monkeypatch.setattr(
            mod.settings, "ECS_CLUSTER_ARN", "arn:aws:ecs:us-east-1:1:cluster/isol8-test", raising=False
        )
        with patch("core.containers.ecs_manager.boto3") as mock_boto3:
            mock_boto3.client.return_value = MagicMock()
            manager = EcsManager()
        monkeypatch.setattr("core.containers.get_ecs_manager", lambda: manager)

        ecs, cluster, service_name = mod._owner_service_view("org_abc")

        assert ecs is manager._ecs
        assert cluster == manager._cluster
        # The derivation is EcsManager's, not a reimplementation here.
        assert service_name == manager._service_name("org_abc")
        assert service_name.startswith("openclaw-")


class TestDeny:
    @pytest.mark.asyncio
    async def test_toggles_denied_flag_on_the_current_token_row(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")
        token = await mod.mint_gate_token("org_abc", _AIPS)
        h = mod.hash_token(token)

        await mod.deny_gate_token("org_abc", True)
        row = gate_tables.get_item(TableName="test-gate-tokens", Key={"token_hash": {"S": h}})["Item"]
        assert row["denied"]["BOOL"] is True

        await mod.deny_gate_token("org_abc", False)
        row = gate_tables.get_item(TableName="test-gate-tokens", Key={"token_hash": {"S": h}})["Item"]
        assert row["denied"]["BOOL"] is False

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

    def test_constructs_per_env_name_by_convention(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_GATE_TOKENS_TABLE", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod", raising=False)
        assert mod._tokens_table_name() == "isol8-prod-bedrock-gate-tokens"

    def test_defaults_env_to_dev_when_unset(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_GATE_TOKENS_TABLE", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "", raising=False)
        assert mod._tokens_table_name() == "isol8-dev-bedrock-gate-tokens"
