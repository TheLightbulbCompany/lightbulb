"""Per-workspace AgentCore runtime provisioning."""

import json

from core.services import agentcore_runtime as ar


class TestRuntimeName:
    def test_is_stable_for_an_owner(self):
        assert ar.runtime_name_for("org_abc") == ar.runtime_name_for("org_abc")

    def test_is_aws_legal(self):
        # Letters, digits and underscores only, starting with a letter.
        name = ar.runtime_name_for("org_3H3viGAfq2SXc9dIdcatmGXg8Td")
        assert name[0].isalpha()
        assert all(c.isalnum() or c == "_" for c in name), name
        assert len(name) <= 48

    def test_owners_sharing_a_sanitised_prefix_do_not_collide(self):
        # The hash is what prevents this. A collision would put two workspaces on ONE runtime,
        # and therefore one EFS mount — the failure this whole design exists to avoid.
        long_a = "org_" + "x" * 60 + "AAAA"
        long_b = "org_" + "x" * 60 + "BBBB"
        assert ar.runtime_name_for(long_a) != ar.runtime_name_for(long_b)

    def test_characters_aws_rejects_are_sanitised(self):
        name = ar.runtime_name_for("org/with:bad.chars-here")
        assert all(c.isalnum() or c == "_" for c in name), name


class TestTrustPolicy:
    ECS_STATEMENT = {
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }

    def test_adds_agentcore_without_removing_ecs(self):
        # Additive is deliberate: a workspace may still be served by its ECS task mid-migration,
        # and dropping that statement under a live task breaks it mid-turn.
        doc = ar.trust_policy_with_agentcore({"Version": "2012-10-17", "Statement": [self.ECS_STATEMENT]}, "1234")
        services = [s["Principal"]["Service"] for s in doc["Statement"]]
        assert "ecs-tasks.amazonaws.com" in services
        assert ar.AGENTCORE_SERVICE_PRINCIPAL in services

    def test_is_idempotent(self):
        first = ar.trust_policy_with_agentcore({"Version": "2012-10-17", "Statement": [self.ECS_STATEMENT]}, "1234")
        second = ar.trust_policy_with_agentcore(first, "1234")
        assert first == second
        assert sum(s["Principal"]["Service"] == ar.AGENTCORE_SERVICE_PRINCIPAL for s in second["Statement"]) == 1

    def test_scopes_the_confused_deputy_fence_like_the_ecs_statement(self):
        doc = ar.trust_policy_with_agentcore(None, "999999")
        added = next(s for s in doc["Statement"] if s["Principal"]["Service"] == ar.AGENTCORE_SERVICE_PRINCIPAL)
        assert added["Condition"]["StringEquals"]["aws:SourceAccount"] == "999999"
        assert "999999" in added["Condition"]["ArnLike"]["aws:SourceArn"]

    def test_does_not_mutate_the_caller_document(self):
        original = {"Version": "2012-10-17", "Statement": [self.ECS_STATEMENT]}
        snapshot = json.dumps(original)
        ar.trust_policy_with_agentcore(original, "1234")
        assert json.dumps(original) == snapshot


class TestRuntimeRequest:
    def _req(self):
        return ar.build_runtime_request(
            owner_id="org_abc",
            image_uri="1234.dkr.ecr.us-east-1.amazonaws.com/gw:v1",
            role_arn="arn:aws:iam::1234:role/lightbulb-dev-owner-bedrock-org_abc",
            efs_access_point_arn="arn:aws:elasticfilesystem:us-east-1:1234:access-point/fsap-1",
            subnets=["subnet-a", "subnet-b"],
            security_groups=["sg-1"],
            idle_timeout_s=14400,
            max_lifetime_s=28800,
        )

    def test_requires_vpc_mode(self):
        # PUBLIC mode cannot reach Cloud Map (Paperclip) or mount the owner's EFS.
        assert self._req()["networkConfiguration"]["networkMode"] == "VPC"

    def test_mounts_the_owners_access_point_at_the_expected_path(self):
        fs = self._req()["filesystemConfigurations"][0]["efsAccessPoint"]
        assert fs["mountPath"] == "/mnt/oc"
        assert fs["accessPointArn"].endswith("fsap-1")

    def test_carries_the_sleep_policy(self):
        life = self._req()["lifecycleConfiguration"]
        assert life["idleRuntimeSessionTimeout"] == 14400  # 4h, the owner's ruling
        assert life["maxLifetime"] == 28800  # AWS hard ceiling

    def test_is_tagged_with_its_owner(self):
        assert self._req()["tags"]["owner_id"] == "org_abc"


class TestRuntimeEnv:
    def _env(self, **kw):
        base = dict(
            owner_id="org_abc",
            api_base="https://api-dev.flickmeon.ai/",
            hook_token="hook-123",
            litestream_bucket="lightbulb-dev-openclaw-replicas",
        )
        base.update(kw)
        return ar.build_runtime_env(**base)

    def test_carries_the_callback_identity_the_gateway_needs(self):
        # Without these the gateway boots but cannot report runs or post agent replies.
        env = self._env()
        assert env["ISOL8_OWNER_ID"] == "org_abc"
        assert env["OPENCLAW_HOOK_TOKEN"] == "hook-123"

    def test_strips_the_trailing_slash_from_the_api_base(self):
        # The plugin concatenates paths onto this; a double slash breaks the hook routes.
        assert self._env()["ISOL8_API_URL"] == "https://api-dev.flickmeon.ai"

    def test_supplies_the_static_block_ecs_gets_from_its_task_definition(self):
        env = self._env()
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "AWS_CONFIG_FILE", "CLAWHUB_WORKDIR"):
            assert key in env, key

    def test_omits_the_browser_variables_since_that_stack_is_stripped(self):
        env = self._env()
        assert "DISPLAY" not in env
        assert "BROWSER_EGRESS_PROXY_PORT" not in env

    def test_disables_respawn_so_a_restart_cannot_kill_the_microvm(self):
        # The update-restart path spawns a detached child and exits, taking litestream -exec
        # with it -- which on ECS killed the task outright on 2026-09-14.
        assert self._env()["OPENCLAW_NO_RESPAWN"] == "1"

    def test_gate_bearer_is_optional_and_omitted_when_absent(self):
        assert "AWS_BEARER_TOKEN_BEDROCK" not in self._env()
        assert self._env(gate_bearer="tok")["AWS_BEARER_TOKEN_BEDROCK"] == "tok"

    def test_request_carries_the_environment(self):
        req = ar.build_runtime_request(
            owner_id="org_abc",
            image_uri="img",
            role_arn="arn:role",
            efs_access_point_arn="arn:ap",
            subnets=["s"],
            security_groups=["sg"],
            idle_timeout_s=14400,
            max_lifetime_s=28800,
            environment=self._env(),
        )
        assert req["environmentVariables"]["ISOL8_OWNER_ID"] == "org_abc"
