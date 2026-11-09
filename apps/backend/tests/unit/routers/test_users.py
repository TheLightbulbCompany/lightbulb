"""Unit tests for users router."""

import pytest
from unittest.mock import AsyncMock, patch

from core.auth import AuthContext


class TestSyncUser:
    """Tests for POST /api/v1/users/sync endpoint."""

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_creates_new_user(self, mock_repo, async_client):
        """Sync creates a new /users row when the user doesn't exist."""
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        assert response.json()["status"] == "created"
        assert response.json()["user_id"] == "user_test_123"

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_returns_exists_for_existing_user(self, mock_repo, async_client):
        """Sync returns 'exists' for existing user."""
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123", "created_at": "2026-01-01T00:00:00Z"})

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        assert response.json()["status"] == "exists"
        assert response.json()["user_id"] == "user_test_123"

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    @patch("routers.users.user_repo")
    async def test_sync_does_not_create_billing_account(
        self, mock_user_repo, mock_stripe, mock_billing_repo, async_client
    ):
        """Regression: /users/sync must NOT create a billing row.

        Creating billing on sync produced phantom personal-context rows when
        ChatLayout mounted during the transient "Clerk session has no active
        org yet" window. Billing is now created only by POST /billing/checkout.
        """
        mock_user_repo.get = AsyncMock(return_value=None)
        mock_user_repo.put = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        # Zero Stripe + repo writes. Sync only touched /users.
        mock_stripe.Customer.create.assert_not_called()
        mock_billing_repo.create_if_not_exists.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_persists_the_jwt_email_on_create(self, mock_repo, mock_auth_context, async_client):
        """The users-table email is what ``launch_referral._email_for_owner``
        reads from inside a Stripe webhook, where no JWT exists. Sync must
        write it rather than leaving the Clerk webhook as its only source."""
        mock_auth_context.email = "test@example.com"
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        mock_repo.put.assert_awaited_once_with("user_test_123", "test@example.com")

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_heals_a_row_with_no_email(self, mock_repo, mock_auth_context, async_client):
        """Rows created while the Clerk webhook pointed at the retired domain
        have no email at all. The next sync fills it in."""
        mock_auth_context.email = "test@example.com"
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123", "created_at": "2026-01-01T00:00:00Z"})
        mock_repo.backfill_email = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        assert response.json()["status"] == "exists"
        mock_repo.backfill_email.assert_awaited_once_with("user_test_123", "test@example.com")

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_leaves_an_existing_email_alone(self, mock_repo, async_client):
        """A row that already carries an email is never rewritten — ``put`` is
        a whole-item overwrite and the backfill is conditional anyway."""
        mock_repo.get = AsyncMock(
            return_value={"user_id": "user_test_123", "email": "real@example.com", "push_tokens": {"d": {}}}
        )
        mock_repo.backfill_email = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        mock_repo.backfill_email.assert_not_awaited()
        mock_repo.put.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_survives_a_failed_backfill(self, mock_repo, async_client):
        """The heal is best-effort: a DDB blip must not fail the sync."""
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_repo.backfill_email = AsyncMock(side_effect=RuntimeError("ddb down"))

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        assert response.json()["status"] == "exists"

    @pytest.mark.asyncio
    async def test_sync_requires_authentication(self, unauthenticated_async_client):
        """Sync requires authentication."""
        response = await unauthenticated_async_client.post("/api/v1/users/sync")
        assert response.status_code in [401, 403]


class TestSyncUserProviderChoice:
    """/users/sync does not write provider_choice.

    /users/sync is a pure user-row sync and ignores legacy provider fields
    so old frontends keep working without advertising or writing them.
    """

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_does_not_persist_provider_choice(self, mock_repo, async_client):
        """provider_choice in the body is silently ignored — no user_repo write."""
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post(
            "/api/v1/users/sync",
            json={"provider_choice": "bedrock_claude"},
        )

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_does_not_persist_byo_key(self, mock_repo, async_client):
        """byo_key + byo_provider in the body are also silently ignored."""
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post(
            "/api/v1/users/sync",
            json={"provider_choice": "byo_key", "byo_provider": "openai"},
        )

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_byo_key_without_provider_no_longer_400s(self, mock_repo, async_client):
        """byo_key-without-byo_provider succeeds (no 400).

        The endpoint silently ignores both fields, so a body that would
        otherwise trigger a 400 just succeeds with no repo write.
        Pydantic Literal validation still rejects unknown values (422).
        """
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post(
            "/api/v1/users/sync",
            json={"provider_choice": "byo_key"},
        )

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_without_provider_choice_unchanged(self, mock_repo, async_client):
        """Existing callers (no body / empty body) still work and bypass set_provider_choice."""
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync", json={})

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_no_body_unchanged(self, mock_repo, async_client):
        """No body at all (legacy contract) still works."""
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_ignores_unknown_legacy_provider_choice(self, mock_repo, async_client):
        """Unknown legacy provider fields are ignored just like the retired values."""
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post(
            "/api/v1/users/sync",
            json={"provider_choice": "wat"},
        )

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()


class TestGetMe:
    """Tests for GET /api/v1/users/me.

    The canonical provider_choice store lives on billing_repo (per-owner).
    For backwards compat with frontend callers (ControlSidebar,
    ControlPanelRouter, LLMPanel, OutOfCreditsBanner), /me still surfaces
    provider_choice and byo_provider — but reads them from billing_repo
    by resolved owner_id.
    """

    @pytest.mark.asyncio
    @patch("routers.users.billing_repo")
    @patch("routers.users.user_repo")
    async def test_me_returns_provider_choice_from_billing_repo(self, mock_user_repo, mock_billing_repo, async_client):
        """provider_choice + byo_provider come from billing_repo, not user_repo."""
        mock_user_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_billing_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "provider_choice": "byo_key",
                "byo_provider": "openai",
            }
        )

        response = await async_client.get("/api/v1/users/me")

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "user_test_123"
        assert body["provider_choice"] == "byo_key"
        assert body["byo_provider"] == "openai"

    @pytest.mark.asyncio
    @patch("routers.users.billing_repo")
    @patch("routers.users.user_repo")
    async def test_me_provider_choice_null_when_no_billing_row(self, mock_user_repo, mock_billing_repo, async_client):
        """No billing row yet -> provider fields surface as null (loading state)."""
        mock_user_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_billing_repo.get_by_owner_id = AsyncMock(return_value=None)

        response = await async_client.get("/api/v1/users/me")

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "user_test_123"
        assert body["provider_choice"] is None
        assert body["byo_provider"] is None

    @pytest.mark.asyncio
    @patch("routers.users.billing_repo")
    @patch("routers.users.user_repo")
    async def test_me_unsynced_user_still_returns_shape(self, mock_user_repo, mock_billing_repo, async_client):
        """Unsynced user (no /users row) still returns the full provider shape."""
        mock_user_repo.get = AsyncMock(return_value=None)
        mock_billing_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "provider_choice": "bedrock_claude",
            }
        )

        response = await async_client.get("/api/v1/users/me")

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "user_test_123"
        assert body["provider_choice"] == "bedrock_claude"
        # byo_provider not set on this row -> serialized as null
        assert body["byo_provider"] is None

    @pytest.mark.asyncio
    @patch("routers.users.billing_repo")
    @patch("routers.users.user_repo")
    async def test_me_bedrock_claude_no_byo_provider(self, mock_user_repo, mock_billing_repo, async_client):
        """bedrock_claude row -> byo_provider is null (not set on the row)."""
        mock_user_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_billing_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "provider_choice": "bedrock_claude",
            }
        )

        response = await async_client.get("/api/v1/users/me")

        assert response.status_code == 200
        body = response.json()
        assert body["provider_choice"] == "bedrock_claude"
        assert body["byo_provider"] is None


class TestPrefs:
    """Tests for GET/PUT /api/v1/users/me/prefs."""

    @staticmethod
    def _wire_real_helpers(mock_repo) -> None:
        """`_prefs()` calls user_repo's pure derivation helpers directly
        (timezone_of/daily_summary_on/chat_theme_of); `@patch("routers.users.user_repo")`
        replaces the whole module with a MagicMock, so those calls return
        unresolved Mocks unless pointed back at the real functions."""
        from core.repositories import user_repo as real_user_repo

        mock_repo.timezone_of = real_user_repo.timezone_of
        mock_repo.daily_summary_on = real_user_repo.daily_summary_on
        mock_repo.chat_theme_of = real_user_repo.chat_theme_of
        mock_repo.sessions_reachable_from_background_work = real_user_repo.sessions_reachable_from_background_work
        mock_repo.session_notifications = real_user_repo.session_notifications

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_get_returns_null_chat_theme_when_never_set(self, mock_repo, async_client):
        """A fresh/unsynced row has no stored theme -> null, not a guessed default.

        The client needs that null to mean "defer to this device's own
        localStorage/OS preference" rather than overwrite it — see
        chat_theme_of's docstring.
        """
        self._wire_real_helpers(mock_repo)
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})

        response = await async_client.get("/api/v1/users/me/prefs")

        assert response.status_code == 200
        assert response.json()["chatTheme"] is None

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_get_returns_the_stored_chat_theme(self, mock_repo, async_client):
        self._wire_real_helpers(mock_repo)
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123", "chat_theme": "dark"})

        response = await async_client.get("/api/v1/users/me/prefs")

        assert response.status_code == 200
        assert response.json()["chatTheme"] == "dark"

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_put_sets_the_chat_theme(self, mock_repo, async_client):
        self._wire_real_helpers(mock_repo)
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123", "chat_theme": "dark"})
        mock_repo.set_prefs = AsyncMock(return_value=None)

        response = await async_client.put("/api/v1/users/me/prefs", json={"chatTheme": "dark"})

        assert response.status_code == 200
        assert response.json()["chatTheme"] == "dark"
        mock_repo.set_prefs.assert_awaited_once_with(
            "user_test_123",
            timezone=None,
            daily_summary=None,
            away_email=None,
            chat_theme="dark",
            sessions_reachable_from_background_work=None,
            session_notifications=None,
        )

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_put_rejects_an_unknown_chat_theme(self, mock_repo, async_client):
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})

        response = await async_client.put("/api/v1/users/me/prefs", json={"chatTheme": "sepia"})

        assert response.status_code == 422
        mock_repo.set_prefs.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_put_omitting_chat_theme_leaves_it_unchanged(self, mock_repo, async_client):
        """PUT with only `timezone` must not clobber a previously-set chatTheme —
        set_prefs only writes the keys it's given (see its docstring)."""
        self._wire_real_helpers(mock_repo)
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123", "chat_theme": "dark"})
        mock_repo.set_prefs = AsyncMock(return_value=None)

        response = await async_client.put("/api/v1/users/me/prefs", json={"timezone": "Europe/Paris"})

        assert response.status_code == 200
        mock_repo.set_prefs.assert_awaited_once_with(
            "user_test_123",
            timezone="Europe/Paris",
            daily_summary=None,
            away_email=None,
            chat_theme=None,
            sessions_reachable_from_background_work=None,
            session_notifications=None,
        )
        # The mocked row already carries chat_theme=dark, so the response still
        # reflects it even though this PUT didn't touch it.
        assert response.json()["chatTheme"] == "dark"

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_the_agent_session_switches_default_off_and_are_set(self, mock_repo, async_client):
        """Background work reaches a person's sessions unless they opt out (owner ruling 2026-10-26)."""
        self._wire_real_helpers(mock_repo)
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_repo.set_prefs = AsyncMock(return_value=None)

        body = (await async_client.get("/api/v1/users/me/prefs")).json()
        assert body["sessionsReachableFromBackgroundWork"] is True and "agentSendWithoutAsking" not in body

        mock_repo.get = AsyncMock(
            return_value={
                "user_id": "user_test_123",
                "sessions_reachable_from_background_work": True,
            }
        )
        response = await async_client.put(
            "/api/v1/users/me/prefs",
            json={"sessionsReachableFromBackgroundWork": True},
        )
        assert response.status_code == 200
        assert response.json()["sessionsReachableFromBackgroundWork"] is True
        kwargs = mock_repo.set_prefs.await_args.kwargs
        assert kwargs["sessions_reachable_from_background_work"] is True

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_session_notifications_default_to_todays_behaviour_and_round_trip(self, mock_repo, async_client):
        self._wire_real_helpers(mock_repo)
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_repo.set_prefs = AsyncMock(return_value=None)

        body = (await async_client.get("/api/v1/users/me/prefs")).json()
        assert body["sessionNotifications"] == {
            "waiting": "banner",
            "finished": "banner",
            "attention": "badge",
            "onlyUnfocused": False,
            "sound": True,
        }

        stored = {"waiting": "badge", "finished": "off", "attention": "banner", "onlyUnfocused": True, "sound": False}
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123", "session_notifications": stored})
        response = await async_client.put("/api/v1/users/me/prefs", json={"sessionNotifications": stored})
        assert response.status_code == 200 and response.json()["sessionNotifications"] == stored
        assert mock_repo.set_prefs.await_args.kwargs["session_notifications"] == stored

        # A level the server does not know is refused, and a stored one reads as its default.
        bad = await async_client.put("/api/v1/users/me/prefs", json={"sessionNotifications": {"waiting": "loud"}})
        assert bad.status_code == 422
        mock_repo.get = AsyncMock(return_value={"user_id": "u", "session_notifications": {"waiting": "loud"}})
        assert (await async_client.get("/api/v1/users/me/prefs")).json()["sessionNotifications"]["waiting"] == "banner"


class TestDeleteMyAccount:
    """Tests for POST /api/v1/users/me/delete (self-serve deletion, 5.1.1(v))."""

    @pytest.fixture
    def mock_auth_context(self) -> AuthContext:
        """A caller with no workspace (the legacy personal shape)."""
        return AuthContext(user_id="user_test_123", email="person@example.com")

    @staticmethod
    def _report():
        from unittest.mock import MagicMock

        report = MagicMock()
        report.to_dict.return_value = {"steps": [{"status": "done"}]}
        return report

    @pytest.mark.asyncio
    @patch("routers.users.admin_actions_repo")
    @patch("core.services.account_wipe.wipe_account", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_deletes_personal_account(self, mock_emails, mock_wipe, mock_audit, async_client):
        """Typed-confirm match runs the wipe for the caller and audits it."""
        mock_emails.return_value = ["Person@Example.com"]
        mock_wipe.return_value = self._report()
        mock_audit.create = AsyncMock()

        response = await async_client.post("/api/v1/users/me/delete", json={"confirm_email": "person@example.com"})

        assert response.status_code == 200
        assert response.json() == {"status": "deleted"}
        mock_wipe.assert_awaited_once_with("user_test_123", actor_admin_id="user_test_123")
        assert mock_audit.create.await_args.kwargs["action"] == "account_wipe_self"
        assert mock_audit.create.await_args.kwargs["target_user_id"] == "user_test_123"

    @pytest.mark.asyncio
    @patch("core.services.account_wipe.wipe_account", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_email_mismatch_never_wipes(self, mock_emails, mock_wipe, async_client):
        """A wrong typed email 400s before any teardown runs."""
        mock_emails.return_value = ["person@example.com"]

        response = await async_client.post("/api/v1/users/me/delete", json={"confirm_email": "wrong@example.com"})

        assert response.status_code == 400
        assert response.json()["detail"] == "email_mismatch"
        mock_wipe.assert_not_awaited()

    @pytest.fixture(autouse=True)
    def clerk_orgs(self):
        """The caller's other orgs (none by default) and their members."""
        with (
            patch("core.services.clerk_admin.list_user_organizations", new_callable=AsyncMock) as orgs,
            patch("core.services.clerk_admin.list_organization_members", new_callable=AsyncMock) as members,
        ):
            orgs.return_value = []
            members.return_value = []
            yield orgs, members

    @pytest.mark.asyncio
    @patch("routers.users.admin_actions_repo")
    @patch("core.services.workspace_wipe.wipe_own_workspace", new_callable=AsyncMock)
    @patch("core.services.account_wipe.wipe_account", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_workspace_with_other_members_deletes_only_the_caller(
        self, mock_emails, mock_wipe, mock_ws_wipe, mock_audit, async_client_org_admin
    ):
        """Others remain: the user-keyed wipe archives the caller's membership and
        deletes their account; the workspace (runtime, company, billing) is untouched."""
        from core.services.workspace_wipe import OtherMembersRemainError

        mock_emails.return_value = ["person@example.com"]
        mock_ws_wipe.side_effect = OtherMembersRemainError("others")
        mock_wipe.return_value = self._report()
        mock_audit.create = AsyncMock()

        response = await async_client_org_admin.post(
            "/api/v1/users/me/delete", json={"confirm_email": "person@example.com"}
        )

        assert response.status_code == 200
        assert response.json() == {"status": "deleted"}
        mock_ws_wipe.assert_awaited_once_with("org_test_456", user_id="user_test_123")
        mock_wipe.assert_awaited_once_with("user_test_123", actor_admin_id="user_test_123")

    @pytest.mark.asyncio
    @patch("routers.users.admin_actions_repo")
    @patch("core.services.workspace_wipe.wipe_own_workspace", new_callable=AsyncMock)
    @patch("core.services.account_wipe.wipe_account", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_sole_member_wipes_the_workspace(
        self, mock_emails, mock_wipe, mock_ws_wipe, mock_audit, async_client_org_admin, clerk_orgs
    ):
        mock_emails.return_value = ["person@example.com"]
        clerk_orgs[0].return_value = [{"id": "org_test_456", "name": "Test"}]
        mock_ws_wipe.return_value = self._report()
        mock_audit.create = AsyncMock()

        response = await async_client_org_admin.post(
            "/api/v1/users/me/delete", json={"confirm_email": "person@example.com"}
        )

        assert response.status_code == 200
        assert response.json() == {"status": "deleted"}
        mock_ws_wipe.assert_awaited_once_with("org_test_456", user_id="user_test_123")
        mock_wipe.assert_not_awaited()
        clerk_orgs[0].assert_awaited_once_with("user_test_123", limit=100, strict=True)

    @pytest.mark.asyncio
    @patch("core.services.workspace_wipe.wipe_own_workspace", new_callable=AsyncMock)
    @patch("core.services.account_wipe.wipe_account", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_sole_member_of_another_org_is_refused(
        self, mock_emails, mock_wipe, mock_ws_wipe, async_client_org_admin, clerk_orgs
    ):
        """Deleting the Clerk user would orphan org B, still billing: refuse, wipe nothing."""
        mock_emails.return_value = ["person@example.com"]
        clerk_orgs[0].return_value = [{"id": "org_test_456", "name": "Test"}, {"id": "org_b", "name": "Acme B"}]
        clerk_orgs[1].return_value = [{"user_id": "user_test_123"}]

        response = await async_client_org_admin.post(
            "/api/v1/users/me/delete", json={"confirm_email": "person@example.com"}
        )

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "You're the only member of Acme B. Delete or hand over that workspace first."
        )
        clerk_orgs[1].assert_awaited_with("org_b", strict=True)
        mock_ws_wipe.assert_not_awaited()
        mock_wipe.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("org_id", ["org_test_456", "org_b"])
    @patch("core.services.workspace_wipe.wipe_own_workspace", new_callable=AsyncMock)
    @patch("core.services.account_wipe.wipe_account", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_last_admin_with_other_members_is_refused(
        self, mock_emails, mock_wipe, mock_ws_wipe, async_client_org_admin, clerk_orgs, org_id
    ):
        """Refuse, don't orphan: the active org or any other org."""
        mock_emails.return_value = ["person@example.com"]
        clerk_orgs[0].return_value = [{"id": org_id, "name": "Acme", "role": "org:admin"}]
        clerk_orgs[1].return_value = [
            {"user_id": "user_test_123", "role": "org:admin"},
            {"user_id": "user_other", "role": "org:member"},
        ]

        response = await async_client_org_admin.post(
            "/api/v1/users/me/delete", json={"confirm_email": "person@example.com"}
        )

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "You're the admin of Acme. Make someone else an admin, or delete the workspace first."
        )
        mock_ws_wipe.assert_not_awaited()
        mock_wipe.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.users.admin_actions_repo")
    @patch("core.services.workspace_wipe.wipe_own_workspace", new_callable=AsyncMock)
    @patch("core.services.account_wipe.wipe_account", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_admin_with_another_admin_or_plain_member_leaves(
        self, mock_emails, mock_wipe, mock_ws_wipe, mock_audit, async_client_org_admin, clerk_orgs
    ):
        """Another admin remains in the active org, plain member elsewhere: the caller just leaves."""
        from core.services.workspace_wipe import OtherMembersRemainError

        mock_emails.return_value = ["person@example.com"]
        clerk_orgs[0].return_value = [
            {"id": "org_test_456", "name": "Test", "role": "org:admin"},
            {"id": "org_b", "name": "B", "role": "org:member"},
        ]
        clerk_orgs[1].return_value = [
            {"user_id": "user_test_123", "role": "org:admin"},
            {"user_id": "user_other", "role": "org:admin"},
        ]
        mock_ws_wipe.side_effect = OtherMembersRemainError("others")
        mock_wipe.return_value = self._report()
        mock_audit.create = AsyncMock()

        response = await async_client_org_admin.post(
            "/api/v1/users/me/delete", json={"confirm_email": "person@example.com"}
        )

        assert response.status_code == 200
        mock_wipe.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("core.services.workspace_wipe.wipe_own_workspace", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_permanent_refusal_is_shown_as_is(self, mock_emails, mock_ws_wipe, async_client_org_admin):
        from core.services.workspace_wipe import SelfServeBlockedError

        mock_emails.return_value = ["person@example.com"]
        mock_ws_wipe.side_effect = SelfServeBlockedError("Retrying won't fix this — contact support.")

        response = await async_client_org_admin.post(
            "/api/v1/users/me/delete", json={"confirm_email": "person@example.com"}
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "Retrying won't fix this — contact support."

    @pytest.mark.asyncio
    @patch("routers.users.admin_actions_repo")
    @patch("core.services.workspace_wipe.wipe_own_workspace", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_sole_member_refusal_asks_for_retry(
        self, mock_emails, mock_ws_wipe, mock_audit, async_client_org_admin
    ):
        """A refusal before any step (e.g. a stale snapshot): 409, retryable."""
        from core.services.workspace_wipe import WorkspaceWipeError

        mock_emails.return_value = ["person@example.com"]
        mock_ws_wipe.side_effect = WorkspaceWipeError("changed")

        response = await async_client_org_admin.post(
            "/api/v1/users/me/delete", json={"confirm_email": "person@example.com"}
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "deletion_incomplete"

    @pytest.mark.asyncio
    @patch("routers.users.admin_actions_repo")
    @patch("core.services.workspace_wipe.wipe_own_workspace", new_callable=AsyncMock)
    @patch("core.services.account_wipe.wipe_account", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_retry_after_partial_success_reports_deleted(
        self, mock_emails, mock_wipe, mock_ws_wipe, mock_audit, async_client_org_admin
    ):
        """Second attempt after rows were already removed: skipped steps are not failures."""
        from unittest.mock import MagicMock

        from core.services.workspace_wipe import OtherMembersRemainError

        mock_emails.return_value = ["person@example.com"]
        mock_ws_wipe.side_effect = OtherMembersRemainError("others")
        report = MagicMock()
        report.to_dict.return_value = {"steps": [{"status": "skipped"}, {"status": "ok"}]}
        mock_wipe.return_value = report
        mock_audit.create = AsyncMock()

        response = await async_client_org_admin.post(
            "/api/v1/users/me/delete", json={"confirm_email": "person@example.com"}
        )

        assert response.status_code == 200
        assert response.json() == {"status": "deleted"}

    @pytest.mark.asyncio
    async def test_requires_authentication(self, unauthenticated_async_client):
        response = await unauthenticated_async_client.post("/api/v1/users/me/delete", json={"confirm_email": "a@b.co"})
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    @patch("routers.users.admin_actions_repo")
    @patch("core.services.account_wipe.wipe_account", new_callable=AsyncMock)
    @patch("core.services.account_wipe.resolve_account_emails", new_callable=AsyncMock)
    async def test_partial_failure_never_reports_deleted(self, mock_emails, mock_wipe, mock_audit, async_client):
        """A failed teardown step 502s (still audited) — never a false 'deleted'."""
        from unittest.mock import MagicMock

        mock_emails.return_value = ["person@example.com"]
        report = MagicMock()
        report.to_dict.return_value = {"steps": [{"status": "done"}, {"status": "failed"}]}
        mock_wipe.return_value = report
        mock_audit.create = AsyncMock()

        response = await async_client.post("/api/v1/users/me/delete", json={"confirm_email": "person@example.com"})

        assert response.status_code == 502
        assert response.json()["detail"] == "deletion_incomplete"
        assert mock_audit.create.await_args.kwargs["result"] == "partial_failure"
