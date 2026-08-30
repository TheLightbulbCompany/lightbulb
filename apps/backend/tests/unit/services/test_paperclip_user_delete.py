"""Unit tests for paperclip_user_delete.delete_paperclip_user — Admin
Account-Wipe Task 2.

Mocks the boto3 rds-data / secretsmanager control-plane clients directly
(MagicMock), mirroring tests/unit/billing/test_bedrock_aip_delete.py's style
of monkeypatching the module's client-factory functions rather than using
moto (which does not model RDS Data API).
"""

from unittest.mock import MagicMock

import pytest

from core.services import paperclip_user_delete as mod


def _secretsmanager_stub(
    env: str = "dev",
    secret_arn: str = "arn:aws:secretsmanager:us-east-1:877352799272:secret:creds-abc",
):
    sm = MagicMock()
    sm.get_paginator.return_value.paginate.return_value = [
        {
            "SecretList": [
                {"Name": f"lightbulb-{env}-paperclip-db-credentials-XYZ", "ARN": secret_arn},
            ]
        }
    ]
    return sm


class TestDeletePaperclipUser:
    @pytest.mark.asyncio
    async def test_issues_delete_by_id_with_correct_sql_and_params(self, monkeypatch):
        sm = _secretsmanager_stub()
        rds = MagicMock()
        monkeypatch.setattr(mod, "_secretsmanager_client", lambda: sm)
        monkeypatch.setattr(mod, "_rds_data_client", lambda: rds)
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)

        await mod.delete_paperclip_user(paperclip_user_id="pc_user_123", email="member@example.com", env="dev")

        rds.execute_statement.assert_called_once()
        kwargs = rds.execute_statement.call_args.kwargs
        assert kwargs["sql"] == 'DELETE FROM "user" WHERE id = :id'
        assert kwargs["database"] == "paperclip"
        assert kwargs["parameters"] == [{"name": "id", "value": {"stringValue": "pc_user_123"}}]
        assert kwargs["resourceArn"] == "arn:aws:rds:us-east-1:877352799272:cluster:lightbulb-dev-paperclip-db"
        assert kwargs["secretArn"] == "arn:aws:secretsmanager:us-east-1:877352799272:secret:creds-abc"

    @pytest.mark.asyncio
    async def test_resolves_prod_cluster_arn_for_prod_env(self, monkeypatch):
        sm = _secretsmanager_stub(env="prod")
        rds = MagicMock()
        monkeypatch.setattr(mod, "_secretsmanager_client", lambda: sm)
        monkeypatch.setattr(mod, "_rds_data_client", lambda: rds)
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)

        await mod.delete_paperclip_user(paperclip_user_id="pc_user_456", email="member2@example.com", env="prod")

        kwargs = rds.execute_statement.call_args.kwargs
        assert kwargs["resourceArn"] == "arn:aws:rds:us-east-1:877352799272:cluster:lightbulb-prod-paperclip-db"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bootstrap_email",
        [
            "admin@isol8.co",
            "admin+paperclip@isol8.co",
            # Case/whitespace variants — the guard normalizes (strip().lower())
            # so a differently-cased bootstrap email can't slip past a direct call.
            "Admin@Isol8.co",
            "  admin@isol8.co  ",
        ],
    )
    async def test_refuses_bootstrap_emails(self, monkeypatch, bootstrap_email):
        rds = MagicMock()
        sm = MagicMock()
        monkeypatch.setattr(mod, "_secretsmanager_client", lambda: sm)
        monkeypatch.setattr(mod, "_rds_data_client", lambda: rds)

        with pytest.raises(mod.BootstrapUserProtectedError):
            await mod.delete_paperclip_user(paperclip_user_id="pc_admin", email=bootstrap_email, env="prod")

        rds.execute_statement.assert_not_called()
        sm.get_paginator.assert_not_called()

    @pytest.mark.asyncio
    async def test_propagates_unexpected_rds_data_errors(self, monkeypatch):
        sm = _secretsmanager_stub()
        rds = MagicMock()
        rds.execute_statement.side_effect = RuntimeError("throttled")
        monkeypatch.setattr(mod, "_secretsmanager_client", lambda: sm)
        monkeypatch.setattr(mod, "_rds_data_client", lambda: rds)

        with pytest.raises(RuntimeError, match="throttled"):
            await mod.delete_paperclip_user(paperclip_user_id="pc_user_789", email="member3@example.com", env="dev")

    @pytest.mark.asyncio
    async def test_raises_when_secret_not_found(self, monkeypatch):
        sm = MagicMock()
        sm.get_paginator.return_value.paginate.return_value = [{"SecretList": []}]
        rds = MagicMock()
        monkeypatch.setattr(mod, "_secretsmanager_client", lambda: sm)
        monkeypatch.setattr(mod, "_rds_data_client", lambda: rds)

        with pytest.raises(RuntimeError, match="Could not find paperclip-db-credentials secret"):
            await mod.delete_paperclip_user(paperclip_user_id="pc_user_x", email="member4@example.com", env="dev")

        rds.execute_statement.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "email",
    [
        "admin@isol8.co",
        "admin+paperclip@isol8.co",
        "admin@thelightbulb.company",
        "admin+paperclip@thelightbulb.company",
        "  Admin+Paperclip@TheLightbulb.Company  ",
    ],
)
async def test_refuses_every_bootstrap_address_across_both_mail_domains(email):
    """Regression: the guard is keyed on the email, but the address it must match
    lives in the paperclip_admin_credentials secret. When that secret moved to
    thelightbulb.company the guard still listed only the isol8.co pair, so the
    account owning every Paperclip company was deletable by an ordinary wipe."""
    from core.services import paperclip_user_delete as mod

    with pytest.raises(mod.BootstrapUserProtectedError):
        await mod.delete_paperclip_user(paperclip_user_id="u_1", email=email, env="prod")
