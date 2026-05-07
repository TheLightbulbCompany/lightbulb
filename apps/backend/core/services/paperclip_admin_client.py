"""Typed httpx async client for Paperclip's REST API.

**Auth model** (2026-05-02 fix): every call that "acts as" a user
carries that user's Better Auth session as a
``Cookie: paperclip-<instance>.session_token=<signed-value>`` header.
Paperclip's Better Auth config (``server/src/auth/better-auth.ts``)
does NOT enable the ``bearer()`` plugin, so ``Authorization: Bearer``
is silently ignored — only the cookie path authenticates. Sign-up /
sign-in extract the ``Set-Cookie`` from Better Auth's response,
strip attributes (``Path=`` / ``Secure`` / ``HttpOnly`` / ``SameSite``),
and surface the bare ``name=value`` as ``_session_cookie`` on the
returned dict. Callers thread that string in as ``session_cookie``
for every subsequent admin call.

The two unauthenticated routes — ``sign_up_user`` and
``sign_in_user`` — hit Better Auth's public endpoints directly and
don't carry any auth at all.

Endpoint surface verified against the local Paperclip checkout at
``~/Desktop/paperclip``:

  - Auth (Better Auth): mounted at ``/api/auth/*`` in
    ``server/src/auth/better-auth.ts``. Standard Better Auth routes —
    ``POST /api/auth/sign-up/email`` and
    ``POST /api/auth/sign-in/email`` accept ``{email, password, name?}``
    and respond ``{user, token}`` with a Set-Cookie session cookie.
  - Companies routes: ``server/src/routes/companies.ts``.
  - Agents routes:    ``server/src/routes/agents.ts``.
  - Access / invite routes: ``server/src/routes/access.ts``:
      * ``POST /api/companies/{companyId}/invites`` — create a
        company-join invite (requires ``users:invite`` permission;
        the user behind ``session_cookie`` must already be a member).
        Body shape: ``createCompanyInviteSchema`` from
        ``packages/shared/src/validators/access.ts:12``:
        ``{allowedJoinTypes: "human"|"agent"|"both", humanRole?, defaultsPayload?, agentMessage?}``.
        Note: invites are NOT email-targeted — they're token-based.
        The returned ``token`` is the credential that any human can
        use to accept.
      * ``POST /api/invites/{token}/accept`` — accept an invite. The
        signed-in actor (Better Auth session) becomes the new
        member. Body: ``acceptInviteSchema`` —
        ``{requestType: "human"|"agent", agentName?, ...}``. For our
        flow we always pass ``requestType: "human"``.
      * ``POST /api/companies/{companyId}/join-requests/{requestId}/approve``
        — approve the resulting pending join request. The signed-in
        actor must hold ``joins:approve`` on the company.

Notable deviations from the original plan template:

  - ``mint_board_api_key`` REMOVED. Paperclip's REST API has no
    per-user board-key minting endpoint; board API keys are only
    mintable via the CLI auth challenge flow
    (``server/src/services/board-auth.ts``). The new auth model
    instead creates a Better Auth account per Isol8 user (see
    ``sign_up_user`` / ``sign_in_user``) and authenticates proxied
    requests with the resulting session token.
  - ``create_company`` body is ``{name, description?, budgetMonthlyCents?}``
    per ``createCompanySchema`` in
    ``packages/shared/src/validators/company.ts``. There is no
    ``ownerEmail`` field — the caller becomes the owner via
    ``access.ensureMembership`` on the server side, using the actor of
    the cookie's user (so for org-create we MUST pass the org-owner's
    ``session_cookie``, not the admin's).
  - ``create_agent`` sends ``adapterType`` as a top-level field separate
    from ``adapterConfig`` — they are distinct fields per
    ``createAgentSchema``.
  - There is no ``disable`` company endpoint; Paperclip only offers
    ``POST /api/companies/{companyId}/archive``. ``disable_company`` is
    therefore mapped onto the archive endpoint.
  - Paperclip does not honor ``Idempotency-Key`` headers today (no
    middleware/route handler reads it). The client still forwards the
    header when callers provide one so that future Paperclip-side
    support can be picked up without changes here.

**Important: ``PAPERCLIP_AUTH_DISABLE_SIGN_UP=true``.**

That env var (read in ``server/src/config.ts``, applied as
``disableSignUp: config.authDisableSignUp`` to Better Auth in
``server/src/auth/better-auth.ts:121``) is enforced by the Better Auth
library itself — when ``true``, the public ``/api/auth/sign-up/email``
route is rejected for ALL callers, including the backend. This means
production cannot call ``sign_up_user`` while the flag is on. The
deployment plan therefore keeps ``PAPERCLIP_AUTH_DISABLE_SIGN_UP`` set
to ``false`` at the Paperclip server level (Paperclip is on a private
subnet, only reachable through our backend, so the public attack
surface is closed at the network edge rather than at the Better Auth
layer). T11 (provisioning) is the one place in the codebase that
calls ``sign_up_user``; it relies on this network-level closure.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx


def _extract_session_cookie(resp: httpx.Response) -> Optional[str]:
    """Pull Better Auth's session cookie from a sign-in/sign-up response.

    Returns ``"<name>=<value>"`` ready to drop into a ``Cookie:`` header,
    or ``None`` if the response carries no Better Auth session cookie.

    Why we parse ``Set-Cookie`` directly instead of reading
    ``resp.cookies.jar``: Paperclip's Better Auth marks the cookie
    ``Secure`` whenever ``PAPERCLIP_PUBLIC_URL`` is https (which it is
    in dev/prod). httpx's cookie jar refuses to STORE Secure cookies
    received over plain HTTP — and our hop to ``paperclip.<env>.local``
    is plain HTTP — so the jar comes back empty. Manual parsing of
    ``Set-Cookie`` lets us capture the value verbatim regardless of
    transport, and replay it to the same upstream where the ``Secure``
    attribute is irrelevant for inbound headers.
    """
    headers_obj = resp.headers
    if hasattr(headers_obj, "get_list"):
        set_cookies = headers_obj.get_list("set-cookie")
    else:
        single = headers_obj.get("set-cookie")
        set_cookies = [single] if single else []
    for raw in set_cookies:
        # Take only the first segment up to the first ``;`` — that's
        # the bare ``name=value`` pair, with attributes like Path,
        # Domain, Secure, HttpOnly, SameSite stripped.
        head = raw.split(";", 1)[0].strip()
        eq = head.find("=")
        if eq <= 0:
            continue
        name = head[:eq]
        if name.endswith(".session_token"):
            return head
    return None


logger = logging.getLogger(__name__)


class PaperclipApiError(Exception):
    """Raised when Paperclip returns a non-2xx response.

    DELETE 404s are NOT raised — callers expect "already gone" to be
    a successful no-op for delete operations.

    The ``retryable`` attribute is auto-classified from ``status_code``:
    5xx (server errors) and 429 (rate-limit) are transient and should
    be retried; everything else (4xx state errors) is permanent and
    must not loop. T12's webhook handler reads ``retryable`` to decide
    whether to enqueue the failed event for async retry.
    """

    def __init__(self, message: str, status_code: int, body: Any):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        # Auto-classify retryability: 5xx + 429 are retryable, everything
        # else (4xx state errors) is not.
        self.retryable: bool = status_code >= 500 or status_code == 429


class PaperclipAdminClient:
    """Async client wrapping the subset of Paperclip's REST API that
    Isol8's provisioning + cleanup paths need.

    The underlying ``httpx.AsyncClient`` is supplied by the caller so
    that it can be shared with other Paperclip-bound code (e.g. proxy
    code in T14/T15) and so its base_url + connection pool can be
    configured once per Paperclip instance.

    Every operation that affects a specific company is invoked with
    a per-user ``session_cookie`` so the actor recorded in Paperclip's
    activity log + permission check is the right user.
    """

    def __init__(self, http_client: httpx.AsyncClient):
        self._http = http_client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(
        self,
        session_cookie: str,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, str]:
        """Build request headers.

        Authenticates the request via ``Cookie: <session_cookie>``
        (where ``session_cookie`` is the ``name=value`` line captured
        from Better Auth's sign-in/sign-up ``Set-Cookie`` response).
        Bearer auth would be ignored — Paperclip does not enable the
        Better Auth ``bearer()`` plugin.

        Also stamps ``Origin: <PAPERCLIP_PUBLIC_URL>`` when set. Why:
        Paperclip's actor middleware calls Better Auth's internal
        ``api.getSession({headers})``, and Better Auth uses the request
        URL (derived from ``Origin``) to decide whether to look for the
        ``__Secure-`` cookie prefix. Without an https Origin it looks
        for the bare cookie name and our admin call lands as
        anonymous → 403 on every privileged endpoint. Verified
        empirically against dev on 2026-05-02. The official handler
        path (``/api/auth/get-session``) doesn't have this dependency
        because it has the real request URL.
        """
        headers = {
            "Cookie": session_cookie,
            "Content-Type": "application/json",
        }
        # Read from the settings singleton (canonical config source —
        # pydantic loads from .env, tests monkeypatch via
        # settings.PAPERCLIP_PUBLIC_URL). Reading os.environ would miss
        # both .env loading and test-suite overrides. Codex P2 on PR #508.
        # Local import keeps the module load lightweight and avoids a
        # circular import for tests that bypass settings entirely.
        from core.config import settings

        if settings.PAPERCLIP_PUBLIC_URL:
            headers["Origin"] = settings.PAPERCLIP_PUBLIC_URL
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _post(
        self,
        path: str,
        json: dict,
        session_cookie: str,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        resp = await self._http.post(
            path,
            json=json,
            headers=self._headers(session_cookie, idempotency_key=idempotency_key),
        )
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"POST {path} -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        return resp.json() if resp.content else {}

    async def _delete(
        self,
        path: str,
        session_cookie: str,
    ) -> dict:
        resp = await self._http.delete(
            path,
            headers=self._headers(session_cookie),
        )
        # 404 on delete is treated as already-gone (idempotent cleanup).
        if resp.status_code >= 400 and resp.status_code != 404:
            raise PaperclipApiError(
                f"DELETE {path} -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        if resp.status_code == 404 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    async def _get(
        self,
        path: str,
        session_cookie: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict:
        resp = await self._http.get(
            path,
            params=params,
            headers=self._headers(session_cookie),
        )
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"GET {path} -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        return resp.json() if resp.content else {}

    async def _patch(
        self,
        path: str,
        json: dict,
        session_cookie: str,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        resp = await self._http.patch(
            path,
            json=json,
            headers=self._headers(session_cookie, idempotency_key=idempotency_key),
        )
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"PATCH {path} -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------
    # Better Auth (per-user accounts)
    # ------------------------------------------------------------------

    async def sign_up_user(
        self,
        *,
        email: str,
        password: str,
        name: Optional[str] = None,
    ) -> dict:
        """Create a Paperclip Better Auth user via ``/api/auth/sign-up/email``.

        Returns the Better Auth response payload, typically
        ``{user: {id, email, name, ...}, token: <session token>}``
        along with a Set-Cookie session cookie on the response (which
        is captured by the proxy router, not this client).

        Auth: this route does NOT require admin auth — anyone can sign
        up. We rely on ``PAPERCLIP_AUTH_DISABLE_SIGN_UP=false`` at the
        Paperclip server (combined with Paperclip being on a private
        subnet behind our backend) to make this safe. See module
        docstring for the network-level posture.
        """
        body: dict[str, Any] = {
            "email": email,
            "password": password,
            "name": name if name is not None else email,
        }
        # NOTE: we do not pass any session — sign-up is unauthenticated.
        resp = await self._http.post(
            "/api/auth/sign-up/email",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"POST /api/auth/sign-up/email -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        data = resp.json() if resp.content else {}
        cookie = _extract_session_cookie(resp)
        if cookie:
            data["_session_cookie"] = cookie
        return data

    async def sign_in_user(self, *, email: str, password: str) -> dict:
        """Sign in via Better Auth ``/api/auth/sign-in/email``.

        Returns the Better Auth ``{user, token}`` JSON augmented with a
        ``_session_cookie`` field — the bare ``name=value`` extracted
        from the response's ``Set-Cookie`` header. ``_session_cookie``
        is what every subsequent admin-client method requires; the raw
        ``token`` field is NOT usable for auth on its own (Paperclip
        doesn't enable Better Auth's bearer plugin). The proxy router
        is also responsible for re-shaping ``Set-Cookie`` for the
        browser path (scoping to ``.isol8.co``); this method only
        cares about the bare cookie value for upstream calls.
        """
        body = {"email": email, "password": password}
        resp = await self._http.post(
            "/api/auth/sign-in/email",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"POST /api/auth/sign-in/email -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        data = resp.json() if resp.content else {}
        cookie = _extract_session_cookie(resp)
        if cookie:
            data["_session_cookie"] = cookie
        return data

    # ------------------------------------------------------------------
    # Companies
    # ------------------------------------------------------------------

    async def create_company(
        self,
        *,
        session_cookie: str,
        name: str,
        description: Optional[str] = None,
        budget_monthly_cents: int = 0,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Create a Paperclip company.

        Maps to ``POST /api/companies`` (see
        ``server/src/routes/companies.ts:267``).

        Server returns ``201`` with the company object including ``id``,
        ``name``, ``description``, ``status``, ``budgetMonthlyCents``,
        ``createdAt``, ``updatedAt``. The session cookie's user is
        automatically granted ``owner`` membership on the new company,
        so callers MUST pass the org-owner's ``session_cookie`` here.
        """
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        if budget_monthly_cents:
            body["budgetMonthlyCents"] = budget_monthly_cents
        return await self._post(
            "/api/companies",
            json=body,
            session_cookie=session_cookie,
            idempotency_key=idempotency_key,
        )

    async def disable_company(
        self,
        *,
        session_cookie: str,
        company_id: str,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Soft-disable a company by archiving it.

        Maps to ``POST /api/companies/{companyId}/archive`` (see
        ``server/src/routes/companies.ts:380``). Paperclip has no
        dedicated ``disable`` endpoint; archive is the documented
        equivalent — hides from default listings, preserves data.
        """
        return await self._post(
            f"/api/companies/{company_id}/archive",
            json={},
            session_cookie=session_cookie,
            idempotency_key=idempotency_key,
        )

    async def delete_company(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> None:
        """Hard-delete a company.

        Maps to ``DELETE /api/companies/{companyId}`` (see
        ``server/src/routes/companies.ts:400``). 404 is swallowed so
        cleanup retries are idempotent.
        """
        await self._delete(
            f"/api/companies/{company_id}",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Invite-flow chain (member onboarding)
    # ------------------------------------------------------------------
    #
    # Paperclip's invite flow is intentionally token-based, not
    # email-based: ``createInvite`` returns a one-shot ``token`` that
    # any human accepting it (via ``acceptInvite``) becomes a
    # join_request for. The receiving member must already be signed
    # in (Better Auth session) before they accept — that's the actor
    # who gets bound to the resulting membership.

    async def create_invite(
        self,
        *,
        session_cookie: str,
        company_id: str,
        email: str,  # noqa: ARG002 - kept for caller-side audit/logging
        human_role: Optional[str] = None,
    ) -> dict:
        """Create a company-join invite as the signed-in admin.

        Maps to ``POST /api/companies/{companyId}/invites``. Body
        shape (per ``createCompanyInviteSchema`` in
        ``packages/shared/src/validators/access.ts:12``)::

            {
              "allowedJoinTypes": "human",   # we never use agent invites here
              "humanRole": "member"|"admin"|null,
              "defaultsPayload": {...}|null,
              "agentMessage": "..."|null
            }

        The response includes ``token`` — the one-shot invite secret
        that a *different* user (the new member) hands back via
        ``accept_invite``. ``email`` is accepted for caller-side
        audit/logging only; Paperclip itself does not bind the invite
        to an email address.
        """
        body: dict[str, Any] = {
            "allowedJoinTypes": "human",
        }
        if human_role is not None:
            body["humanRole"] = human_role
        return await self._post(
            f"/api/companies/{company_id}/invites",
            json=body,
            session_cookie=session_cookie,
        )

    async def accept_invite(
        self,
        *,
        session_cookie: str,
        invite_token: str,
    ) -> dict:
        """Accept an invite as the new member (signed in via session_cookie).

        Maps to ``POST /api/invites/{token}/accept`` (see
        ``server/src/routes/access.ts:3199``). For human accept the
        body is just ``{requestType: "human"}``; the resulting
        ``join_request`` is returned and starts in
        ``status: "pending_approval"`` until an existing admin
        approves it.
        """
        return await self._post(
            f"/api/invites/{invite_token}/accept",
            json={"requestType": "human"},
            session_cookie=session_cookie,
        )

    async def approve_join_request(
        self,
        *,
        session_cookie: str,
        company_id: str,
        request_id: str,
    ) -> dict:
        """Approve a pending join request as a board admin.

        Maps to ``POST /api/companies/{companyId}/join-requests/{requestId}/approve``
        (see ``server/src/routes/access.ts:3697``). The signed-in
        user behind ``session_cookie`` must hold ``joins:approve`` on
        the company (every owner does by default).
        """
        return await self._post(
            f"/api/companies/{company_id}/join-requests/{request_id}/approve",
            json={},
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    async def create_agent(
        self,
        *,
        session_cookie: str,
        company_id: str,
        name: str,
        role: str,
        adapter_type: str,
        adapter_config: dict,
        title: Optional[str] = None,
        capabilities: Optional[str] = None,
        reports_to: Optional[str] = None,
        budget_monthly_cents: int = 0,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Create an agent (employee) inside a company.

        Maps to ``POST /api/companies/{companyId}/agents`` (see
        ``server/src/routes/agents.ts:1634``). ``adapterType`` and
        ``adapterConfig`` are sibling fields on the request body per
        ``createAgentSchema`` in
        ``packages/shared/src/validators/agent.ts:47``.
        """
        body: dict[str, Any] = {
            "name": name,
            "role": role,
            "adapterType": adapter_type,
            "adapterConfig": adapter_config,
        }
        if title is not None:
            body["title"] = title
        if capabilities is not None:
            body["capabilities"] = capabilities
        if reports_to is not None:
            body["reportsTo"] = reports_to
        if budget_monthly_cents:
            body["budgetMonthlyCents"] = budget_monthly_cents
        return await self._post(
            f"/api/companies/{company_id}/agents",
            json=body,
            session_cookie=session_cookie,
            idempotency_key=idempotency_key,
        )

    async def create_agent_api_key(
        self,
        *,
        session_cookie: str,
        agent_id: str,
        name: str = "default",
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Mint a long-lived API key for an agent.

        Maps to ``POST /api/agents/{agentId}/keys`` (see
        ``server/src/routes/agents.ts:2334``). The response includes the
        plaintext ``token`` exactly once — callers MUST store it
        immediately; it is hashed at rest server-side.

        Response shape (per ``createApiKey`` in
        ``server/src/services/agents.ts:607``)::

            {
              "id": "...",
              "name": "default",
              "token": "<plaintext key value>",
              "createdAt": "..."
            }

        This is the closest persistent agent-level credential
        Paperclip's REST API exposes — board API keys are only
        mintable via the CLI auth challenge flow, so the per-agent
        key created here is what T11 (provisioning) hands the seeded
        CEO agent.
        """
        return await self._post(
            f"/api/agents/{agent_id}/keys",
            json={"name": name},
            session_cookie=session_cookie,
            idempotency_key=idempotency_key,
        )

    async def list_agents(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """List agents in a company.

        Maps to ``GET /api/companies/{companyId}/agents``.
        """
        return await self._get(
            f"/api/companies/{company_id}/agents",
            session_cookie=session_cookie,
        )

    async def get_agent(
        self,
        *,
        session_cookie: str,
        agent_id: str,
    ) -> dict:
        """Fetch a single agent by id.

        Maps to ``GET /api/agents/{agentId}``.
        """
        return await self._get(
            f"/api/agents/{agent_id}",
            session_cookie=session_cookie,
        )

    async def patch_agent(
        self,
        *,
        session_cookie: str,
        agent_id: str,
        body: dict,
    ) -> dict:
        """Patch an agent.

        Maps to ``PATCH /api/agents/{agentId}``. The body is a
        whitelisted subset built by the BFF — adapter fields are
        synthesized server-side and never accepted from clients.
        """
        return await self._patch(
            f"/api/agents/{agent_id}",
            json=body,
            session_cookie=session_cookie,
        )

    async def delete_agent(
        self,
        *,
        session_cookie: str,
        agent_id: str,
    ) -> dict:
        """Delete an agent.

        Maps to ``DELETE /api/agents/{agentId}``. 404 is swallowed
        (treated as already-gone) by the underlying ``_delete`` helper.
        """
        return await self._delete(
            f"/api/agents/{agent_id}",
            session_cookie=session_cookie,
        )

    async def list_runs(
        self,
        *,
        session_cookie: str,
        agent_id: str,
    ) -> dict:
        """List runs for an agent.

        Maps to ``GET /api/agents/{agentId}/runs``.
        """
        return await self._get(
            f"/api/agents/{agent_id}/runs",
            session_cookie=session_cookie,
        )

    async def get_run(
        self,
        *,
        session_cookie: str,
        run_id: str,
    ) -> dict:
        """Fetch a single run by id.

        Maps to ``GET /api/runs/{runId}``.
        """
        return await self._get(
            f"/api/runs/{run_id}",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Inbox
    # ------------------------------------------------------------------

    async def list_inbox_for_session_user(
        self,
        *,
        session_cookie: str,
        params: Optional[dict[str, Any]] = None,
    ) -> list:
        """List the signed-in agent's inbox-lite issue rows.

        Maps to ``GET /api/agents/me/inbox-lite``. The optional ``params``
        dict is forwarded as query string so the BFF can pass through
        filter selections (tab, status, project, assignee, creator, search,
        limit) verbatim. Empty/None params is allowed for back-compat with
        the existing tier-1 caller.
        """
        return await self._get(
            "/api/agents/me/inbox-lite",
            session_cookie=session_cookie,
            params=params,
        )

    async def dismiss_inbox_item(
        self,
        *,
        session_cookie: str,
        item_id: str,
    ) -> dict:
        """Dismiss a single inbox item.

        Maps to ``POST /api/inbox/{itemId}/dismiss``.
        """
        return await self._post(
            f"/api/inbox/{item_id}/dismiss",
            json={},
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Heartbeat runs (NEW for #3a — Inbox deep port)
    # ------------------------------------------------------------------

    async def list_company_heartbeat_runs(
        self,
        *,
        session_cookie: str,
        company_id: str,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """List heartbeat runs for the company.

        Maps to ``GET /api/companies/{companyId}/heartbeat-runs``. The
        ``status`` filter (e.g. "failed") is forwarded as a query param
        so the BFF's Inbox "Runs" tab can show only failed runs.
        """
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = str(limit)
        return await self._get(
            f"/api/companies/{company_id}/heartbeat-runs",
            session_cookie=session_cookie,
            params=params or None,
        )

    async def list_company_live_runs(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """List currently-running heartbeat runs for the company.

        Maps to ``GET /api/companies/{companyId}/live-runs``. Used by the
        Inbox UI to show a pulsing "Live" badge on issues with active runs.
        """
        return await self._get(
            f"/api/companies/{company_id}/live-runs",
            session_cookie=session_cookie,
        )

    async def get_heartbeat_run(
        self,
        *,
        session_cookie: str,
        run_id: str,
    ) -> dict:
        """Fetch a single heartbeat run by id.

        Maps to ``GET /api/heartbeat-runs/{runId}``. Used by the agent-run
        detail page that Inbox failed-run rows link into.
        """
        return await self._get(
            f"/api/heartbeat-runs/{run_id}",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------

    async def list_approvals(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """List pending approvals for the company.

        Maps to ``GET /api/companies/{companyId}/approvals``.
        """
        return await self._get(
            f"/api/companies/{company_id}/approvals",
            session_cookie=session_cookie,
        )

    async def approve_approval(
        self,
        *,
        session_cookie: str,
        approval_id: str,
        note: Optional[str],
    ) -> dict:
        """Approve a pending approval, optionally with a reviewer note.

        Maps to ``POST /api/approvals/{approvalId}/approve``. The body
        is intentionally narrow — ``note`` only — to close the
        ``payload.adapterType`` smuggling carrier flagged in the audit.
        """
        body: dict[str, Any] = {"note": note} if note else {}
        return await self._post(
            f"/api/approvals/{approval_id}/approve",
            json=body,
            session_cookie=session_cookie,
        )

    async def reject_approval(
        self,
        *,
        session_cookie: str,
        approval_id: str,
        reason: str,
    ) -> dict:
        """Reject a pending approval. ``reason`` is required upstream.

        Maps to ``POST /api/approvals/{approvalId}/reject``.
        """
        return await self._post(
            f"/api/approvals/{approval_id}/reject",
            json={"reason": reason},
            session_cookie=session_cookie,
        )

    async def get_approval(
        self,
        *,
        session_cookie: str,
        approval_id: str,
    ) -> dict:
        """Fetch a single approval by id.

        Maps to ``GET /api/approvals/{id}``. Used by the approval detail
        page that Inbox approval rows link into.
        """
        return await self._get(
            f"/api/approvals/{approval_id}",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def list_issues(
        self,
        *,
        session_cookie: str,
        company_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict:
        """List issues for the company.

        Maps to ``GET /api/companies/{companyId}/issues``. Optional
        ``params`` is forwarded as the upstream query string so callers
        (BFF inbox listing) can pass through filters like
        ``touchedByUserId=me&inboxArchivedByUserId=me&status=...``.
        """
        return await self._get(
            f"/api/companies/{company_id}/issues",
            session_cookie=session_cookie,
            params=params,
        )

    async def get_issue(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Fetch a single issue by id.

        Maps to ``GET /api/issues/{issueId}``.
        """
        return await self._get(
            f"/api/issues/{issue_id}",
            session_cookie=session_cookie,
        )

    async def create_issue(
        self,
        *,
        session_cookie: str,
        company_id: str,
        body: dict,
    ) -> dict:
        """Create an issue. Body is whitelisted by the BFF.

        Maps to ``POST /api/companies/{companyId}/issues``.
        """
        return await self._post(
            f"/api/companies/{company_id}/issues",
            json=body,
            session_cookie=session_cookie,
        )

    async def patch_issue(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Patch an issue. Body is whitelisted by the BFF.

        Maps to ``PATCH /api/issues/{issueId}``.
        """
        return await self._patch(
            f"/api/issues/{issue_id}",
            json=body,
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Issue inbox state (NEW for #3a)
    # ------------------------------------------------------------------

    async def archive_issue(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Archive an issue from the inbox.

        Maps to ``POST /api/issues/{id}/inbox-archive``. The 49-line BFF
        stub previously had a ``dismiss`` endpoint that mapped to
        ``/api/inbox/{itemId}/dismiss`` — that's a different upstream
        concept (inbox dismissals table) and we keep it. ``archive_issue``
        is the modern Paperclip flow that flips an issue's
        ``inbox_archived_at`` column.
        """
        return await self._post(
            f"/api/issues/{issue_id}/inbox-archive",
            json={},
            session_cookie=session_cookie,
        )

    async def unarchive_issue(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Restore an archived issue back to the inbox.

        Maps to ``DELETE /api/issues/{id}/inbox-archive``. Drives the
        Inbox UI's undo-archive toast.
        """
        return await self._delete(
            f"/api/issues/{issue_id}/inbox-archive",
            session_cookie=session_cookie,
        )

    async def mark_issue_read(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Mark an issue as read for the signed-in user.

        Maps to ``POST /api/issues/{id}/read``.
        """
        return await self._post(
            f"/api/issues/{issue_id}/read",
            json={},
            session_cookie=session_cookie,
        )

    async def mark_issue_unread(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Mark an issue as unread for the signed-in user.

        Maps to ``DELETE /api/issues/{id}/read``.
        """
        return await self._delete(
            f"/api/issues/{issue_id}/read",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Issue comments (NEW for #3a)
    # ------------------------------------------------------------------

    async def list_issue_comments(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """List comments on an issue.

        Maps to ``GET /api/issues/{id}/comments``.
        """
        return await self._get(
            f"/api/issues/{issue_id}/comments",
            session_cookie=session_cookie,
        )

    async def add_issue_comment(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Add a comment to an issue. Body is whitelisted by the BFF.

        Maps to ``POST /api/issues/{id}/comments``.
        """
        return await self._post(
            f"/api/issues/{issue_id}/comments",
            json=body,
            session_cookie=session_cookie,
        )

    async def list_company_labels(
        self,
        *,
        company_id: str,
        session_cookie: str,
    ) -> list[dict]:
        """List labels defined on a Paperclip company.

        Maps to ``GET /api/companies/{companyId}/labels`` upstream. Returns
        a list of ``{id, name, color}`` dicts. Used by the Teams Inbox filter
        chip dropdown to populate selectable labels for the issues filter.

        Returns an empty list if upstream returns a non-list payload (defensive
        — upstream's labels endpoint is documented to return a JSON array per
        ``paperclip/server/src/routes/labels.ts``).
        """
        result = await self._get(
            f"/api/companies/{company_id}/labels",
            session_cookie=session_cookie,
        )
        # Upstream returns a bare list `res.json(labels)` per
        # paperclip/server/src/routes/labels.ts. If shape ever changes,
        # tests fail loudly rather than silently guessing an envelope key.
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Routines
    # ------------------------------------------------------------------

    async def list_routines(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """List routines for the company.

        Maps to ``GET /api/companies/{companyId}/routines``.
        """
        return await self._get(
            f"/api/companies/{company_id}/routines",
            session_cookie=session_cookie,
        )

    async def create_routine(
        self,
        *,
        session_cookie: str,
        company_id: str,
        body: dict,
    ) -> dict:
        """Create a routine. Body is whitelisted by the BFF.

        Maps to ``POST /api/companies/{companyId}/routines``.
        """
        return await self._post(
            f"/api/companies/{company_id}/routines",
            json=body,
            session_cookie=session_cookie,
        )

    async def patch_routine(
        self,
        *,
        session_cookie: str,
        routine_id: str,
        body: dict,
    ) -> dict:
        """Patch a routine. Body is whitelisted by the BFF.

        Maps to ``PATCH /api/routines/{routineId}``.
        """
        return await self._patch(
            f"/api/routines/{routine_id}",
            json=body,
            session_cookie=session_cookie,
        )

    async def delete_routine(
        self,
        *,
        session_cookie: str,
        routine_id: str,
    ) -> dict:
        """Delete a routine. 404 is swallowed as already-gone.

        Maps to ``DELETE /api/routines/{routineId}``.
        """
        return await self._delete(
            f"/api/routines/{routine_id}",
            session_cookie=session_cookie,
        )

    async def run_routine(
        self,
        *,
        routine_id: str,
        body: dict,
        session_cookie: str,
    ) -> dict:
        """Manually fire a routine.

        Maps to ``POST /api/routines/{routineId}/run`` upstream
        (paperclip/server/src/routes/routines.ts:280). Body is forwarded
        as-is to upstream, which validates against ``runRoutineSchema``
        (optional ``reason``, ``inputOverrides``, etc.).

        Returns the upstream response, typically ``{runId, status}``.
        """
        return await self._post(
            f"/api/routines/{routine_id}/run",
            json=body,
            session_cookie=session_cookie,
        )

    async def list_routine_runs(
        self,
        *,
        routine_id: str,
        session_cookie: str,
    ) -> list[dict]:
        """List run history for a routine.

        Maps to ``GET /api/routines/{routineId}/runs`` upstream
        (paperclip/server/src/routes/routines.ts:150). Returns a list of
        run objects (id, status, startedAt, completedAt, etc.).
        """
        result = await self._get(
            f"/api/routines/{routine_id}/runs",
            session_cookie=session_cookie,
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Goals
    # ------------------------------------------------------------------

    async def list_goals(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """List goals for the company.

        Maps to ``GET /api/companies/{companyId}/goals``.
        """
        return await self._get(
            f"/api/companies/{company_id}/goals",
            session_cookie=session_cookie,
        )

    async def create_goal(
        self,
        *,
        session_cookie: str,
        company_id: str,
        body: dict,
    ) -> dict:
        """Create a goal. Body is whitelisted by the BFF.

        Maps to ``POST /api/companies/{companyId}/goals``.
        """
        return await self._post(
            f"/api/companies/{company_id}/goals",
            json=body,
            session_cookie=session_cookie,
        )

    async def patch_goal(
        self,
        *,
        session_cookie: str,
        goal_id: str,
        body: dict,
    ) -> dict:
        """Patch a goal. Body is whitelisted by the BFF.

        Maps to ``PATCH /api/goals/{goalId}``.
        """
        return await self._patch(
            f"/api/goals/{goal_id}",
            json=body,
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    async def list_projects(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """List projects for the company.

        Maps to ``GET /api/companies/{companyId}/projects``.
        """
        return await self._get(
            f"/api/companies/{company_id}/projects",
            session_cookie=session_cookie,
        )

    async def get_project(
        self,
        *,
        session_cookie: str,
        project_id: str,
    ) -> dict:
        """Fetch a single project by id.

        Maps to ``GET /api/projects/{projectId}``.
        """
        return await self._get(
            f"/api/projects/{project_id}",
            session_cookie=session_cookie,
        )

    async def create_project(
        self,
        *,
        session_cookie: str,
        company_id: str,
        body: dict,
    ) -> dict:
        """Create a project. Body is whitelisted by the BFF.

        Maps to ``POST /api/companies/{companyId}/projects``.
        """
        return await self._post(
            f"/api/companies/{company_id}/projects",
            json=body,
            session_cookie=session_cookie,
        )

    async def patch_project(
        self,
        *,
        session_cookie: str,
        project_id: str,
        body: dict,
    ) -> dict:
        """Patch a project. Body is whitelisted by the BFF.

        Maps to ``PATCH /api/projects/{projectId}``.
        """
        return await self._patch(
            f"/api/projects/{project_id}",
            json=body,
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Read-only feed: activity, costs, dashboard, sidebar badges
    # ------------------------------------------------------------------

    async def list_activity(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """List company activity events.

        Maps to ``GET /api/companies/{companyId}/activity``.
        """
        return await self._get(
            f"/api/companies/{company_id}/activity",
            session_cookie=session_cookie,
        )

    async def get_costs(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """Fetch the company costs summary.

        Maps to ``GET /api/companies/{companyId}/costs``.
        """
        return await self._get(
            f"/api/companies/{company_id}/costs",
            session_cookie=session_cookie,
        )

    async def get_dashboard(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """Fetch the dashboard aggregate.

        Maps to ``GET /api/companies/{companyId}/dashboard``.
        """
        return await self._get(
            f"/api/companies/{company_id}/dashboard",
            session_cookie=session_cookie,
        )

    async def get_sidebar_badges(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """Fetch the sidebar badge counts (inbox, approvals, etc).

        Maps to ``GET /api/companies/{companyId}/sidebar-badges``.
        """
        return await self._get(
            f"/api/companies/{company_id}/sidebar-badges",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Skills (read-only)
    # ------------------------------------------------------------------

    async def list_skills(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """List skills available to the company.

        Maps to ``GET /api/companies/{companyId}/skills``.
        """
        return await self._get(
            f"/api/companies/{company_id}/skills",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Members (joined with Clerk in the BFF)
    # ------------------------------------------------------------------

    async def list_members(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """List company memberships.

        Maps to ``GET /api/companies/{companyId}/members``. Email and
        display name are NOT in the upstream response — the BFF joins
        the principal id against Clerk to enrich the rows.
        """
        return await self._get(
            f"/api/companies/{company_id}/members",
            session_cookie=session_cookie,
        )

    async def archive_member(
        self,
        *,
        session_cookie: str,
        company_id: str,
        member_id: str,
    ) -> dict:
        """Archive a single company membership.

        Maps to ``POST /api/companies/{companyId}/members/{memberId}/archive``
        (see ``paperclip/server/src/routes/access.ts:4230``). The
        signed-in user behind ``session_cookie`` must hold
        ``users:manage_permissions`` on the company. ``member_id`` is
        the Paperclip ``company_membership.id`` (NOT the Better Auth
        user id) — callers typically resolve it from ``list_members``
        by matching ``principalId`` to the user's
        ``paperclip_user_id``.

        Returns the archived membership row plus a
        ``reassignedIssueCount`` field. We don't currently do anything
        with the latter; preserved in the return for symmetry with the
        upstream response.
        """
        return await self._post(
            f"/api/companies/{company_id}/members/{member_id}/archive",
            json={},
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Company settings
    # ------------------------------------------------------------------

    async def get_company(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> dict:
        """Fetch the company record.

        Maps to ``GET /api/companies/{companyId}``.
        """
        return await self._get(
            f"/api/companies/{company_id}",
            session_cookie=session_cookie,
        )

    async def patch_company(
        self,
        *,
        session_cookie: str,
        company_id: str,
        body: dict,
    ) -> dict:
        """Patch the company record. Body is whitelisted by the BFF
        to ``display_name`` and ``description`` only.

        Maps to ``PATCH /api/companies/{companyId}``.
        """
        return await self._patch(
            f"/api/companies/{company_id}",
            json=body,
            session_cookie=session_cookie,
        )
