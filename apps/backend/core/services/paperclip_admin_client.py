"""Typed httpx async client for Paperclip's REST API.

**Auth model**: every call that "acts as" a user carries that user's
Better Auth session as a
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

Endpoint surface verified against the Isol8 Paperclip FORK
(``Isol8AI/paperclip``) — what we actually deploy. The vanilla
``paperclipai/paperclip`` clone can diverge; don't verify against it.
Paths below are fork-relative:

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
        ``packages/shared/src/validators/access.ts``:
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
  - Agent-create idempotency uses one canonical ``Idempotency-Key`` header.
    Cap-safe recovery adds ``Idempotency-Replay-Only: true`` without changing
    that logical operation key.

**Important: ``PAPERCLIP_AUTH_DISABLE_SIGN_UP=true``.**

That env var (read in ``server/src/config.ts``, applied as
``disableSignUp: config.authDisableSignUp`` to Better Auth in
``server/src/auth/better-auth.ts``) is enforced by the Better Auth
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

import json as json_lib
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

from core.observability.metrics import put_metric
from core.services.paperclip_adapter_config import coding_runtime_config

# A Paperclip issue id is a UUID or an LB-12-style identifier (`_` admitted for test ids).
# Pinning it keeps a percent-decoded `?`/`&`/`/` from rewriting the fixed upstream path an id
# is interpolated into. The Teams BFF's `IssueId` path type and work_products both use this.
ISSUE_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


def _emit_upstream_error(kind: str) -> None:
    """Best-effort ``paperclip.upstream.error{kind}`` emit for the W63 alarm.

    ``kind`` is a bounded dim ("unreachable" | "5xx"). A metrics failure must
    never mask the upstream error this fires alongside, so it swallows.
    """
    try:
        put_metric("paperclip.upstream.error", dimensions={"kind": kind})
    except Exception:  # noqa: BLE001 — telemetry must not break the request path
        logger.debug("paperclip.upstream.error metric emission failed", exc_info=True)


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


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """The upstream's back-off hint, in seconds, or None.

    BOTH spellings are real, from two different emitters, so read both — a
    caller that checked only one would silently ignore the other's hint:

    - ``Retry-After`` (the standard) — Paperclip's own routes, set via
      ``res.setHeader("Retry-After", ...)`` (fork
      ``server/src/routes/{access,issues}.ts``).
    - ``X-Retry-After`` — Better Auth's rate limiter, which guards
      ``/api/auth/sign-in/email`` (``better-auth/dist/api/rate-limiter/index.mjs``).

    Both emit an integer count of seconds; neither emits the HTTP-date form, so
    parsing one is not worth the code. A malformed or non-positive value is
    treated as absent rather than trusted.
    """
    for name in ("retry-after", "x-retry-after"):
        raw = resp.headers.get(name)
        if not raw:
            continue
        try:
            seconds = float(raw)
        except ValueError:
            continue
        if seconds > 0:
            return seconds
    return None


def _paperclip_error_code(body: Any) -> str | None:
    """Return a structured Paperclip error code without text matching."""

    try:
        payload = json_lib.loads(body) if isinstance(body, str) else body
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    return code if isinstance(code, str) else None


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

    ``message`` is only "<VERB> <path> -> <code>" — it says WHAT failed
    but never WHY. Paperclip puts the reason in the response body, so
    ``__str__`` appends a truncated body: every existing
    ``logger.exception``/``str(exc)`` call site then records the reason
    without a new log line. Callers that forward text to a client must
    use ``.message``, NOT ``str(exc)`` — see ``main.py``'s handler.
    """

    #: Cap on the body appended by ``__str__``. Paperclip 4xx bodies are
    #: ``{"error": ..., "details": [...]}``; a zod issue list fits well
    #: inside this, and the cap stops a large upstream payload from
    #: flooding a log record.
    BODY_LOG_CHARS = 800

    def __init__(self, message: str, status_code: int, body: Any, retry_after: float | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body
        # Auto-classify retryability: 5xx + 429 are retryable, everything
        # else (4xx state errors) is not.
        self.retryable: bool = status_code >= 500 or status_code == 429
        #: Seconds the server asked us to wait, when it sent a hint. Only the
        #: rate-limited routes send one, so this is None on almost every error,
        #: and None just means "no hint, use your own backoff".
        #:
        #: TWO independent rate limiters feed this, which is why
        #: ``_retry_after_seconds`` reads two header spellings: Paperclip's own
        #: routes (via the verb helpers below) and Better Auth's sign-in
        #: limiter (via ``sign_in_user``). ``sign_up_user`` and the attachment
        #: helpers still build their own request and leave this None.
        self.retry_after: float | None = retry_after

    def __str__(self) -> str:
        if not self.body:
            return self.message
        body = str(self.body)
        if len(body) > self.BODY_LOG_CHARS:
            body = body[: self.BODY_LOG_CHARS] + "...[truncated]"
        return f"{self.message}: {body}"


class PaperclipAgentCreateReplayMiss(PaperclipApiError):
    """Replay-only create found no prior operation for its stable key."""


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
        idempotency_replay_only: bool = False,
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
        empirically against dev. The official handler path
        (``/api/auth/get-session``) doesn't have this dependency
        because it has the real request URL.
        """
        headers = {
            "Cookie": session_cookie,
            "Content-Type": "application/json",
        }
        # Read from the settings singleton (canonical config source —
        # pydantic loads from .env, tests monkeypatch via
        # settings.PAPERCLIP_PUBLIC_URL). Reading os.environ would miss
        # both .env loading and test-suite overrides.
        # Local import keeps the module load lightweight and avoids a
        # circular import for tests that bypass settings entirely.
        from core.config import settings

        if settings.PAPERCLIP_PUBLIC_URL:
            headers["Origin"] = settings.PAPERCLIP_PUBLIC_URL
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if idempotency_replay_only:
            headers["Idempotency-Replay-Only"] = "true"
        return headers

    async def _send(self, send: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
        """Run an httpx request, signalling Paperclip-down on a transport error.

        On an ``httpx.RequestError`` (connect/transport failure — Paperclip is
        unreachable) emits ``paperclip.upstream.error{kind:"unreachable"}`` for
        the W63 alarm and re-raises (never swallows the original error). A
        ``status_code >= 500`` response is handled by the per-verb branches via
        ``_emit_upstream_error("5xx")``.
        """
        try:
            return await send()
        except httpx.RequestError:
            _emit_upstream_error("unreachable")
            raise

    async def _post(
        self,
        path: str,
        json: dict,
        session_cookie: str,
        idempotency_key: Optional[str] = None,
        idempotency_replay_only: bool = False,
    ) -> dict:
        resp = await self._send(
            lambda: self._http.post(
                path,
                json=json,
                headers=self._headers(
                    session_cookie,
                    idempotency_key=idempotency_key,
                    idempotency_replay_only=idempotency_replay_only,
                ),
            )
        )
        if resp.status_code >= 500:
            _emit_upstream_error("5xx")
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"POST {path} -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
                retry_after=_retry_after_seconds(resp),
            )
        return resp.json() if resp.content else {}

    async def _delete(
        self,
        path: str,
        session_cookie: str,
    ) -> dict:
        resp = await self._send(
            lambda: self._http.delete(
                path,
                headers=self._headers(session_cookie),
            )
        )
        if resp.status_code >= 500:
            _emit_upstream_error("5xx")
        # 404 on delete is treated as already-gone (idempotent cleanup).
        if resp.status_code >= 400 and resp.status_code != 404:
            raise PaperclipApiError(
                f"DELETE {path} -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
                retry_after=_retry_after_seconds(resp),
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
        resp = await self._send(
            lambda: self._http.get(
                path,
                params=params,
                headers=self._headers(session_cookie),
            )
        )
        if resp.status_code >= 500:
            _emit_upstream_error("5xx")
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"GET {path} -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
                retry_after=_retry_after_seconds(resp),
            )
        return resp.json() if resp.content else {}

    async def _patch(
        self,
        path: str,
        json: dict,
        session_cookie: str,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        resp = await self._send(
            lambda: self._http.patch(
                path,
                json=json,
                headers=self._headers(session_cookie, idempotency_key=idempotency_key),
            )
        )
        if resp.status_code >= 500:
            _emit_upstream_error("5xx")
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"PATCH {path} -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
                retry_after=_retry_after_seconds(resp),
            )
        return resp.json() if resp.content else {}

    async def _put(
        self,
        path: str,
        json: dict,
        session_cookie: str,
        idempotency_key: Optional[str] = None,
    ) -> tuple[int, dict]:
        """PUT verb helper. Returns ``(status_code, body)``.

        Unlike the other verbs, the caller needs the upstream status code: the
        document-upsert route answers ``201`` on create vs ``200`` on update,
        and the BFF forwards that distinction to the client. So this returns
        the code alongside the parsed body rather than the body alone.
        """
        resp = await self._send(
            lambda: self._http.put(
                path,
                json=json,
                headers=self._headers(session_cookie, idempotency_key=idempotency_key),
            )
        )
        if resp.status_code >= 500:
            _emit_upstream_error("5xx")
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"PUT {path} -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
                retry_after=_retry_after_seconds(resp),
            )
        return resp.status_code, (resp.json() if resp.content else {})

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
                retry_after=_retry_after_seconds(resp),
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
        ``server/src/routes/companies.ts``).

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

    async def delete_company(
        self,
        *,
        session_cookie: str,
        company_id: str,
    ) -> None:
        """Hard-delete a company.

        Maps to ``DELETE /api/companies/{companyId}`` (see
        ``server/src/routes/companies.ts``). 404 is swallowed so
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
        ``packages/shared/src/validators/access.ts``)::

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
        ``server/src/routes/access.ts``). For human accept the
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
        (see ``server/src/routes/access.ts``). The signed-in
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
        icon: Optional[str] = None,
        reports_to: Optional[str] = None,
        budget_monthly_cents: int = 0,
        idempotency_key: Optional[str] = None,
        idempotency_replay_only: bool = False,
        can_create_agents: bool = False,
        runtime_config: Optional[dict] = None,
    ) -> dict:
        """Create an agent (employee) inside a company.

        Maps to ``POST /api/companies/{companyId}/agents`` (see
        ``server/src/routes/agents.ts``). ``adapterType`` and
        ``adapterConfig`` are sibling fields on the request body per
        ``createAgentSchema`` in
        ``packages/shared/src/validators/agent.ts``.

        ``permissions.canCreateAgents`` is ALWAYS sent: Paperclip v2026.916
        defaults an omitted value to TRUE, which would let every new agent hire
        agents with its own key. Only the starter CEO passes True. This method
        is the one door every Lightbulb agent-create path goes through.
        """
        body: dict[str, Any] = {
            "name": name,
            "role": role,
            "adapterType": adapter_type,
            "adapterConfig": adapter_config,
            "permissions": {"canCreateAgents": can_create_agents},
        }
        if title is not None:
            body["title"] = title
        if capabilities is not None:
            body["capabilities"] = capabilities
        if icon is not None:
            body["icon"] = icon
        if reports_to is not None:
            body["reportsTo"] = reports_to
        if budget_monthly_cents:
            body["budgetMonthlyCents"] = budget_monthly_cents
        # An engineer is born with its run cap; the reconcile's adapter sync covers a later role change.
        runtime_config = coding_runtime_config(role, runtime_config) or runtime_config
        if runtime_config is not None:
            body["runtimeConfig"] = runtime_config
        if idempotency_replay_only:
            if not idempotency_key:
                raise ValueError("idempotency_replay_only requires idempotency_key")
        try:
            agent = await self._post(
                f"/api/companies/{company_id}/agents",
                json=body,
                session_cookie=session_cookie,
                idempotency_key=idempotency_key,
                idempotency_replay_only=idempotency_replay_only,
            )
        except PaperclipApiError as exc:
            if (
                idempotency_replay_only
                and exc.status_code == 409
                and _paperclip_error_code(exc.body) == "agent_create_idempotency_replay_miss"
            ):
                raise PaperclipAgentCreateReplayMiss(
                    str(exc),
                    status_code=exc.status_code,
                    body=exc.body,
                ) from exc
            raise
        await self._mirror(agent, session_cookie=session_cookie)
        return agent

    async def _mirror(self, response: Any, *, session_cookie: str, agent_id: str | None = None) -> None:
        """Mirror an agent write into the super-agent registry (``agent_registry``; never raises).
        Re-reads the agent when the response is not the full agent dict."""
        from core.services import agent_registry

        agent = response if isinstance(response, dict) and response.get("id") else None
        if agent is None and isinstance(response, dict) and isinstance(response.get("agent"), dict):
            agent = response["agent"]
        try:
            if agent is None or not (agent.get("companyId") and agent.get("name")):
                target = (agent or {}).get("id") or agent_id
                if not target:
                    return
                agent = await self.get_agent(session_cookie=session_cookie, agent_id=target)
        except Exception:  # noqa: BLE001 -- the Paperclip write already succeeded
            logger.warning("agent_registry mirror: re-read failed agent=%s", agent_id, exc_info=True)
            put_metric("agent_registry.mirror", dimensions={"result": "error"})
            return
        await agent_registry.mirror_from_paperclip(agent)

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
        ``server/src/routes/agents.ts``). The response includes the
        plaintext ``token`` exactly once — callers MUST store it
        immediately; it is hashed at rest server-side.

        Response shape (per ``createApiKey`` in
        ``server/src/services/agents.ts``)::

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

    async def revoke_agent_api_key(self, *, session_cookie: str, agent_id: str, key_id: str) -> dict:
        """Revoke one agent API key. Maps to ``DELETE /api/agents/{agentId}/keys/{keyId}``."""
        return await self._delete(f"/api/agents/{agent_id}/keys/{key_id}", session_cookie)

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
        agent = await self._patch(
            f"/api/agents/{agent_id}",
            json=body,
            session_cookie=session_cookie,
        )
        await self._mirror(agent, session_cookie=session_cookie, agent_id=agent_id)
        return agent

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
        result = await self._delete(
            f"/api/agents/{agent_id}",
            session_cookie=session_cookie,
        )
        from core.services import agent_registry

        await agent_registry.mirror_delete(agent_id)
        return result

    async def pause_agent(self, *, session_cookie: str, agent_id: str) -> dict:
        """Pause an agent (stops it running on events).

        Maps to ``POST /api/agents/{agentId}/pause`` (upstream
        ``paperclip/server/src/routes/agents.ts``). Not company-scoped —
        Paperclip authorizes from the session cookie.
        """
        agent = await self._post(
            f"/api/agents/{agent_id}/pause",
            json={},
            session_cookie=session_cookie,
        )
        await self._mirror(agent, session_cookie=session_cookie, agent_id=agent_id)
        return agent

    async def resume_agent(self, *, session_cookie: str, agent_id: str) -> dict:
        """Resume a paused agent. Maps to ``POST /api/agents/{agentId}/resume``."""
        agent = await self._post(
            f"/api/agents/{agent_id}/resume",
            json={},
            session_cookie=session_cookie,
        )
        await self._mirror(agent, session_cookie=session_cookie, agent_id=agent_id)
        return agent

    async def reset_agent_session(
        self,
        *,
        session_cookie: str,
        agent_id: str,
        body: Optional[dict] = None,
    ) -> dict:
        """Reset a wedged agent's runtime session state (recovery op).

        Maps to ``POST /api/agents/{agentId}/runtime-state/reset-session``
        (upstream ``resetAgentSessionSchema``). ``body`` carries an optional
        ``taskKey`` to scope the reset to a single task's session; an empty
        body resets all of the agent's runtime sessions. Board-gated upstream —
        the BFF's per-user session cookie is a board actor.
        """
        return await self._post(
            f"/api/agents/{agent_id}/runtime-state/reset-session",
            json=body or {},
            session_cookie=session_cookie,
        )

    async def clear_agent_error(self, *, session_cookie: str, agent_id: str) -> dict:
        """Clear an agent's error status (recovery op).

        Maps to ``POST /api/agents/{agentId}/clear-error``. Upstream flips an
        ``error``-status agent back to ``idle`` and clears its ``errorReason``;
        it 409s if the agent is not in ``error`` status (or is terminated /
        pending_approval), which ``_post`` surfaces as a ``PaperclipApiError``.
        Returns the updated agent record.
        """
        return await self._post(
            f"/api/agents/{agent_id}/clear-error",
            json={},
            session_cookie=session_cookie,
        )

    async def update_agent(
        self,
        *,
        session_cookie: str,
        agent_id: str,
        role: Optional[str] = None,
        title: Optional[str] = None,
        capabilities: Optional[str] = None,
        icon: Optional[str] = None,
        metadata: Optional[dict] = None,
        status: Optional[str] = None,
    ) -> dict:
        """Partial-update an agent.

        ``status`` is on ``updateAgentSchema`` (but NOT ``createAgentSchema``),
        so it is the only way to put a freshly-created agent into
        ``pending_approval`` — the inert state used while a hire awaits the
        owner's decision (it is in both Paperclip's ``NON_ASSIGNABLE`` and
        ``NON_INVOKABLE`` sets, so it cannot be given work or woken).

        Maps to ``PATCH /api/agents/{agentId}`` (see
        ``paperclip/server/src/routes/agents.ts`` —
        ``updateAgentSchema = createAgentSchema.partial()``). The route
        is not company-scoped: Paperclip authorizes the caller against
        the agent's own company server-side from the session cookie, so
        no ``companyId`` belongs in the path. Only the fields explicitly
        supplied are sent; ``None`` means "leave alone", an empty string
        would WIPE the field server-side.

        Returns the updated agent dict from Paperclip's response so
        callers can read the new state without a follow-up GET.

        Raises :class:`PaperclipApiError` on 4xx/5xx (mirrors the
        existing pattern in ``create_agent`` / ``delete_agent`` via
        the shared ``_patch`` helper).
        """
        body: dict[str, Any] = {}
        if role is not None:
            body["role"] = role
        if title is not None:
            body["title"] = title
        if capabilities is not None:
            body["capabilities"] = capabilities
        if icon is not None:
            body["icon"] = icon
        if metadata is not None:
            body["metadata"] = metadata
        if status is not None:
            body["status"] = status
        agent = await self._patch(
            f"/api/agents/{agent_id}",
            json=body,
            session_cookie=session_cookie,
        )
        await self._mirror(agent, session_cookie=session_cookie, agent_id=agent_id)
        return agent

    # ------------------------------------------------------------------
    # Heartbeat runs
    # ------------------------------------------------------------------

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
    # Attention
    #
    # The unified "what needs a human" feed. Upstream gates it as
    # BOARD-ONLY (``assertBoard`` — agent actors are 403'd) and scopes it
    # to ``req.actor.userId``, because the per-user dismissal/snooze state
    # is joined into every row. It MUST therefore be called with the
    # caller's per-user Paperclip session cookie, never an instance-admin
    # cookie: an admin cookie would both bypass the upstream company scope
    # (cross-tenant IDOR) and resolve dismissals against the wrong user.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Inbox dismissals (per-user: always the caller's own session cookie)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------

    async def list_approvals(
        self,
        *,
        session_cookie: str,
        company_id: str,
        status: Optional[str] = None,
    ) -> dict:
        """List the company's approvals (every status unless ``status`` narrows it).

        Maps to ``GET /api/companies/{companyId}/approvals`` (``?status=`` upstream).
        """
        return await self._get(
            f"/api/companies/{company_id}/approvals",
            session_cookie=session_cookie,
            params={"status": status} if status else None,
        )

    async def create_approval(
        self,
        *,
        session_cookie: str,
        company_id: str,
        body: dict,
    ) -> dict:
        """Create a pending approval.

        Maps to ``POST /api/companies/{companyId}/approvals``. ``body`` carries
        ``type``, ``payload``, and optionally ``issueIds`` — upstream links
        those to the approval via ``issueApprovalsSvc.linkManyForApproval``,
        which is what makes a request show up ON the issue that prompted it
        rather than only in a global list.

        For ``type: "hire_agent"`` upstream additionally runs
        ``normalizeHireApprovalPayloadForPersistence`` over the payload, and
        returns the created row with its payload redacted.
        """
        return await self._post(
            f"/api/companies/{company_id}/approvals",
            json=body,
            session_cookie=session_cookie,
        )

    # ponytail: delete after the prod data move (task 3) (scripts/cancel_paperclip_approvals.py)
    async def reject_approval(
        self,
        *,
        session_cookie: str,
        approval_id: str,
        reason: str,
    ) -> dict:
        """Reject a pending approval with a required reviewer reason.

        Maps to ``POST /api/approvals/{approvalId}/reject``. Upstream's
        ``resolveApprovalSchema`` accepts only ``decisionNote``, so the
        BFF's ``reason`` input is remapped to ``decisionNote`` here —
        sending ``reason`` would be silently stripped and the rejection
        reason lost.

        Rejecting a hire approval makes Paperclip terminate ``payload.agentId`` on its side
        (``marketplace_hire``), so that agent is re-read and mirrored into the registry.
        """
        rejected = await self._post(
            f"/api/approvals/{approval_id}/reject",
            json={"decisionNote": reason},
            session_cookie=session_cookie,
        )
        payload = rejected.get("payload") if isinstance(rejected, dict) else None
        if isinstance(payload, dict) and payload.get("agentId"):
            await self._mirror(None, session_cookie=session_cookie, agent_id=payload["agentId"])
        return rejected

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

    # ------------------------------------------------------------------
    # Plan-document review — upsert / revisions / restore / annotations.
    #
    # These map to the issue-document review surface upstream exposes
    # (``paperclip/server/src/routes/issues.ts``): board edits create new
    # revisions, historical revisions can be restored, and inline annotation
    # THREADS (with comments + a resolve/reopen lifecycle) anchor to a text
    # selection in a specific revision. Every call is issue-scoped; upstream
    # asserts the session cookie's company owns the issue, so a cross-tenant
    # issue id is rejected upstream — the BFF adds a boundary doc-key check on
    # top (see ``routers/teams/issues.py``).
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Issue work-products (typed deliverables + review lifecycle)
    #
    # Work-products are an agent's first-class deliverables — preview_url /
    # pull_request / artifact / document / … — each carrying a reviewState
    # (none | needs_board_review | approved | changes_requested). Distinct
    # from raw attachments: an attachment is a file, a work-product is the
    # reviewable thing the agent produced. Surfaced on the issue-detail
    # "Output" section + the company Artifacts gallery.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Company search (cross-entity: issues / comments / documents /
    # artifacts / agents / projects)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Issue-thread interactions
    #
    # The resolution routes (accept / reject / respond / cancel) are
    # board-only (agents are 403'd) and company-scoped against the SESSION
    # ACTOR upstream. They MUST be called with the caller's per-user
    # Paperclip session cookie (``ctx.session_cookie``), NOT an
    # instance-admin cookie — an admin cookie would bypass the upstream
    # company scope and reach any tenant's interaction (cross-tenant IDOR).
    # ------------------------------------------------------------------

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
    # Issue inbox state
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Company secrets. Board + company access upstream (``server/src/routes/
    # secrets.ts``); the one caller is ``paperclip_github_token``, as the
    # bootstrap admin. Values go IN only: no route here returns one.
    # ------------------------------------------------------------------

    async def delete_secret(self, *, session_cookie: str, secret_id: str) -> dict:
        """Maps to ``DELETE /api/secrets/{id}`` — a soft delete that frees the name.
        404 is already-gone (``_delete``)."""
        return await self._delete(
            f"/api/secrets/{secret_id}",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # External objects (GitHub PRs / issues an issue mentions, with live
    # status). Gated upstream by the instance flag ``enableExternalObjects``:
    # while it is off the lists come back empty, not as an error.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Issue comments
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Issue feedback votes (thumbs up/down on agent output)
    #
    # The only reinforcement signal Paperclip feeds back into agents. Votes
    # are board-only upstream; the BFF's session cookie is a board session so
    # these succeed. Trace listings are consent-gated by the caller
    # (feedbackDataSharingEnabled on the company) — see routers/teams/feedback.py.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Decisions (agent-proposed choices a board user answers)
    #
    # Decide / dismiss are board-only upstream and run the chosen option's
    # effects AS the deciding user, so these always carry the caller's own
    # session cookie — never the admin one.
    # ------------------------------------------------------------------

    async def get_decision(
        self,
        *,
        session_cookie: str,
        decision_id: str,
    ) -> dict:
        """Fetch one decision plus its per-effect ``executions``.

        Maps to ``GET /api/decisions/{id}``.
        """
        return await self._get(
            f"/api/decisions/{decision_id}",
            session_cookie=session_cookie,
        )

    # (The feedback-traces consent gate reads ``feedbackDataSharingEnabled``
    # via the existing ``get_company`` method defined in the Company settings
    # section below.)

    # ------------------------------------------------------------------
    # Issue attachments
    # ------------------------------------------------------------------

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

    async def list_routine_runs(
        self,
        *,
        routine_id: str,
        session_cookie: str,
    ) -> list[dict]:
        """List run history for a routine.

        Maps to ``GET /api/routines/{routineId}/runs`` upstream
        (paperclip/server/src/routes/routines.ts). Returns a list of
        run objects matching upstream ``RoutineRunSummary`` (id, status,
        source, triggeredAt, completedAt, failureReason, linkedIssue,
        trigger). Note: upstream uses ``triggeredAt`` (not ``startedAt``).
        """
        result = await self._get(
            f"/api/routines/{routine_id}/runs",
            session_cookie=session_cookie,
        )
        return result if isinstance(result, list) else []

    async def get_routine(
        self,
        *,
        session_cookie: str,
        routine_id: str,
    ) -> dict:
        """Fetch a single routine by id (the rich RoutineDetail).

        Maps to ``GET /api/routines/{routineId}``.
        """
        return await self._get(
            f"/api/routines/{routine_id}",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Routine triggers + revisions (A2 — editable routines)
    #
    # Trigger mutations are keyed by the GLOBALLY-unique trigger id upstream
    # (``/api/routine-triggers/{id}``), NOT nested under the routine — the BFF
    # nests them under ``/routines/{id}/triggers/{tid}`` only for a RESTful UI
    # shape and passes just the trigger id here.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Goals
    # ------------------------------------------------------------------

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

    async def delete_goal(
        self,
        *,
        session_cookie: str,
        goal_id: str,
    ) -> dict:
        """Delete a goal.

        Maps to ``DELETE /api/goals/{goalId}``. ``_delete`` treats 404 as
        already-gone, so a double-delete from a stale UI is a no-op.
        """
        return await self._delete(
            f"/api/goals/{goal_id}",
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

    async def delete_project(
        self,
        *,
        session_cookie: str,
        project_id: str,
    ) -> dict:
        """Delete a project.

        Maps to ``DELETE /api/projects/{projectId}``. ``_delete`` treats 404
        as already-gone, so a double-delete from a stale UI is a no-op.
        """
        return await self._delete(
            f"/api/projects/{project_id}",
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
        limit: Optional[int] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> dict:
        """List company activity events (optionally one entity's).

        Maps to ``GET /api/companies/{companyId}/activity``. ``limit`` is
        forwarded to upstream's ``normalizeActivityLimit`` (default 100,
        max 500); without it the feed silently caps at 100 instead of the
        200 the UI asks for. ``entity_type``/``entity_id`` become upstream's
        ``entityType``/``entityId`` filters.
        """
        params: dict[str, str] = {}
        if limit is not None:
            params["limit"] = str(limit)
        if entity_type is not None:
            params["entityType"] = entity_type
        if entity_id is not None:
            params["entityId"] = entity_id
        return await self._get(
            f"/api/companies/{company_id}/activity",
            session_cookie=session_cookie,
            params=params or None,
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
        (see ``paperclip/server/src/routes/access.ts``). The
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
        to ``name`` and ``description`` only.

        Maps to ``PATCH /api/companies/{companyId}``, governed by
        ``updateCompanySchema``. That schema strips unknown keys
        silently, so the body's keys must match Paperclip's field names
        exactly or the patch no-ops without raising.
        """
        return await self._patch(
            f"/api/companies/{company_id}",
            json=body,
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Issue tree-control (operator safety lever)
    #
    # Freeze / resume / cancel an entire issue subtree, plus the read-only
    # subtree/blocker diagnostics that complement it. The tree-hold /
    # tree-control routes are board-only (``assertBoard`` in
    # server/src/routes/issue-tree-control.ts); the two diagnostics routes
    # (subtree / blockers) instead use issue-read authz
    # (``assertIssueReadAllowed`` in server/src/routes/issues.ts) — still
    # company-scoped + read-only, just not board-only. Either way these are
    # scoped against the SESSION ACTOR, so they MUST be called with the
    # caller's per-user Paperclip session cookie (``ctx.session_cookie``),
    # never an instance-admin cookie.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Cases (structured work-product records; upstream
    # ``server/src/routes/cases.ts``). Gated upstream by the instance flag
    # ``enableCases`` — while it is off every call answers 403 "Cases are
    # disabled". ``case_id`` may be the uuid or the ``LB-C7`` identifier.
    # Only the calls the Teams Cases panels make are wrapped here.
    # ------------------------------------------------------------------


# Module-level shared httpx client. Created lazily on first use, reused for
# the lifetime of the process. Not closed explicitly — the OS reclaims socket
# FDs at process exit and the connection pool releases idle connections via
# httpx's own keepalive logic. A per-request ``httpx.AsyncClient`` leaks a
# file descriptor + connection-pool task on every call.
_shared_http_client: httpx.AsyncClient | None = None
_shared_admin: PaperclipAdminClient | None = None


def shared_admin_client() -> PaperclipAdminClient:
    """Return the process-wide shared admin client.

    The Teams routers reach this through ``routers.teams.agents._admin`` (the
    monkeypatchable indirection the unit suite patches); non-router callers
    (``main.py`` startup wiring) import it from here directly so no service
    or startup code has to import from ``routers/``.
    """
    global _shared_http_client, _shared_admin
    if _shared_admin is None:
        from core.config import settings

        _shared_http_client = httpx.AsyncClient(
            base_url=settings.PAPERCLIP_INTERNAL_URL,
            timeout=15.0,
        )
        _shared_admin = PaperclipAdminClient(http_client=_shared_http_client)
    return _shared_admin
