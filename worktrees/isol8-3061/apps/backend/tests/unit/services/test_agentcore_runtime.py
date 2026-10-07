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


class TestRequestMatchesTheRealServiceModel:
    """No mock: the request we build must validate against the botocore model that ships in
    the lock. The first dev flip 500'd because the pinned botocore (1.42.68) predated
    ``filesystemConfigurations`` and every other test mocked the client away."""

    def test_create_agent_runtime_request_validates_against_botocore(self):
        import boto3
        from botocore.validate import validate_parameters

        from core.services.hermes_provisioning import build_hermes_runtime_request

        request = build_hermes_runtime_request(
            owner_id="org_abc",
            image_uri="1234.dkr.ecr.us-east-1.amazonaws.com/hermes:v5",
            role_arn="arn:aws:iam::999999999999:role/r",
            subnets=["subnet-a"],
            security_groups=["sg-1"],
            environment={"ISOL8_OWNER_ID": "org_abc"},
        )
        shape = (
            boto3.client("bedrock-agentcore-control", region_name="us-east-1")
            .meta.service_model.operation_model("CreateAgentRuntime")
            .input_shape
        )
        validate_parameters(request, shape)  # raises ParamValidationError on any unknown/invalid member
        # The two members that were the actual 500: a botocore predating either one rejects every
        # runtime we create. (``filesystemConfigurations`` is no longer sent -- a Hermes profile is
        # a complete S3-resident $HERMES_HOME, not an EFS mount.)
        assert "lifecycleConfiguration" in shape.members
        assert "networkConfiguration" in shape.members
