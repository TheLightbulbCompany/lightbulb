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
        deployment_defs: list[str] | None = None,
        task_def_tokens: dict[str, str | None] | None = None,
        raises: Exception | None = None,
    ):
        self.service_task_def = service_task_def
        self.service_status = service_status
        self.service_missing = service_missing
        # Each task: {"taskDefinitionArn", "desiredStatus", "lastStatus"}
        self.tasks = tasks or []
        # Task defs of the service's listed deployments (PRIMARY + any
        # superseded one still draining). Defaults to just the service's own.
        self.deployment_defs = deployment_defs
        self.task_def_tokens = task_def_tokens or {}
        self.raises = raises
        self.calls: list[str] = []

    def describe_services(self, *, cluster, services):
        self.calls.append("describe_services")
        if self.raises:
            raise self.raises
        if self.service_missing:
            return {"services": [], "failures": [{"arn": services[0], "reason": "MISSING"}]}
        svc = {"serviceName": services[0], "status": self.service_status}
        if self.service_task_def:
            svc["taskDefinition"] = self.service_task_def
        defs = self.deployment_defs if self.deployment_defs is not None else [self.service_task_def]
        svc["deployments"] = [
            {"id": f"ecs-svc/{i}", "status": "PRIMARY" if i == 0 else "ACTIVE", "taskDefinition": d}
            for i, d in enumerate(defs)
            if d
        ]
        return {"services": [svc], "failures": []}

    def list_tasks(self, *, cluster, serviceName, desiredStatus):
        self.calls.append(f"list_tasks:{desiredStatus}")
        arns = [
            f"arn:aws:ecs:us-east-1:1:task/{i}" for i, t in enumerate(self.tasks) if t["desiredStatus"] == desiredStatus
        ]
        return {"taskArns": arns}

    def describe_tasks(self, *, cluster, tasks):
        self.calls.append("describe_tasks")
        wanted = set(tasks)
        return {
            "tasks": [
                {
                    "taskArn": f"arn:aws:ecs:us-east-1:1:task/{i}",
                    "taskDefinitionArn": t["taskDefinitionArn"],
                    "lastStatus": t["lastStatus"],
                }
                for i, t in enumerate(self.tasks)
                if f"arn:aws:ecs:us-east-1:1:task/{i}" in wanted
            ]
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


class TestExpireSuperseded:
    """The liveness predicate: a token row lives until nothing can present it.

    Every case is a real scenario. The container gets its token ONLY as an
    inline env var in an ECS task definition, frozen at task start, so the
    authoritative question is what the SERVICE's task definition carries — what
    will launch next — with live tasks as the second half for the drain window.
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
    async def test_rolling_deploy_keeps_the_token_while_the_old_task_drains(self, gate_tables, fake_ecs):
        """The service has moved to the new def, but the old task is still
        RUNNING while ECS drains it (desiredStatus=STOPPED, lastStatus=RUNNING)
        and is still making inference calls with token A."""
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
    async def test_superseded_deployment_alone_keeps_the_token(self, gate_tables, fake_ecs):
        """Second read on the same drain window, free from describe_services:
        ECS still lists the superseded deployment while its tasks wind down, so
        the token survives even if the task listing shows nothing (a page of
        stopped history could otherwise crowd the draining task out)."""
        old_hash, new_hash = self._arrange(
            gate_tables,
            fake_ecs,
            service_def=_NEW_DEF,
            tasks=[],
        )
        fake_ecs.deployment_defs = [_NEW_DEF, _OLD_DEF]  # PRIMARY + draining ACTIVE

        await mod.expire_superseded_gate_tokens("org_abc")

        assert {r["token_hash"]["S"] for r in _token_rows(gate_tables)} == {old_hash, new_hash}

    @pytest.mark.asyncio
    async def test_rolling_deploy_expires_once_the_old_task_has_stopped(self, gate_tables, fake_ecs):
        """Same shape, one moment later: the drained task reached STOPPED, so
        nothing can present A any more."""
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
