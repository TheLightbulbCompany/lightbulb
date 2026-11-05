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
from urllib.parse import quote

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
        """Mirror an agent write into the super-agent registry (``agent_registry``; no-op unless the env
        master is on, never raises). Re-reads the agent when the response is not the full agent dict."""
        from core.services.super_agent_flags import env_enabled  # lazy, like _headers' settings

        if not env_enabled():
            return
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

    async def set_agent_heartbeat(self, *, session_cookie: str, agent_id: str, heartbeat: dict) -> dict:
        """Merge ``heartbeat`` into the agent's ``runtimeConfig.heartbeat``.

        Read-modify-write because ``PATCH /api/agents/{id}`` REPLACES
        ``runtimeConfig`` wholesale (fork ``routes/agents.ts`` PATCH handler; only
        ``aiConnection`` is carried over), so a bare heartbeat patch would drop
        every other runtimeConfig key and the heartbeat keys we do not own
        (``maxConcurrentRuns``). Skips the PATCH when nothing would change.
        """
        agent = await self.get_agent(session_cookie=session_cookie, agent_id=agent_id)
        runtime_config = dict(agent.get("runtimeConfig") or {})
        current = runtime_config.get("heartbeat") or {}
        merged = {**current, **heartbeat}
        if merged == current:
            return agent
        runtime_config["heartbeat"] = merged
        return await self.patch_agent(
            session_cookie=session_cookie, agent_id=agent_id, body={"runtimeConfig": runtime_config}
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
        result = await self._delete(
            f"/api/agents/{agent_id}",
            session_cookie=session_cookie,
        )
        from core.services.super_agent_flags import env_enabled  # lazy, like _headers' settings

        if env_enabled():
            from core.services import agent_registry

            await agent_registry.mirror_delete(agent_id)
        return result

    async def get_budgets_overview(self, *, session_cookie: str, company_id: str) -> dict:
        """``GET /api/companies/{id}/budgets/overview`` — policies + spend per scope."""
        return await self._get(f"/api/companies/{quote(company_id, safe='')}/budgets/overview", session_cookie)

    async def get_costs_summary(
        self, *, session_cookie: str, company_id: str, params: Optional[dict[str, Any]] = None
    ) -> dict:
        """``GET /api/companies/{id}/costs/summary`` (``from``/``to`` date range)."""
        return await self._get(
            f"/api/companies/{quote(company_id, safe='')}/costs/summary", session_cookie, params=params or None
        )

    async def update_company_budget(self, *, session_cookie: str, company_id: str, budget_monthly_cents: int) -> dict:
        """``PATCH /api/companies/{id}/budgets``."""
        return await self._patch(
            f"/api/companies/{quote(company_id, safe='')}/budgets",
            {"budgetMonthlyCents": budget_monthly_cents},
            session_cookie,
        )

    async def update_agent_budget(self, *, session_cookie: str, agent_id: str, budget_monthly_cents: int) -> dict:
        """``PATCH /api/agents/{id}/budgets``."""
        return await self._patch(
            f"/api/agents/{quote(agent_id, safe='')}/budgets",
            {"budgetMonthlyCents": budget_monthly_cents},
            session_cookie,
        )

    async def post_cost_event(
        self,
        *,
        session_cookie: str,
        company_id: str,
        agent_id: str,
        model: str,
        cost_cents: int,
        occurred_at: str,
        provider: str = "openrouter",
    ) -> dict:
        """POST an agent-scoped priced cost event (whole cents; fork ``createCostEventSchema``)."""
        return await self._post(
            f"/api/companies/{quote(company_id, safe='')}/cost-events",
            {
                "agentId": agent_id,
                "provider": provider,
                "model": model,
                "costCents": cost_cents,
                "occurredAt": occurred_at,
            },
            session_cookie,
        )

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

    async def wake_agent(
        self,
        *,
        session_cookie: str,
        agent_id: str,
        body: Optional[dict] = None,
    ) -> dict:
        """Wake an agent to (re)attempt work.

        Maps to ``POST /api/agents/{agentId}/wakeup`` (upstream
        ``wakeAgentSchema``). The body's ``source``/``triggerDetail``/``reason``
        all default upstream, so an empty wake is valid; the failed-run Retry
        passes ``{"source": "on_demand", "reason": "retry"}`` to re-run the
        agent on demand.
        """
        return await self._post(
            f"/api/agents/{agent_id}/wakeup",
            json=body or {"source": "on_demand"},
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

    async def list_company_heartbeat_runs(
        self,
        *,
        session_cookie: str,
        company_id: str,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        agent_id: Optional[str] = None,
        summary: bool = False,
    ) -> dict:
        """List heartbeat runs for the company (optionally one agent's, ``agentId``).

        Maps to ``GET /api/companies/{companyId}/heartbeat-runs``. The
        ``status`` filter (e.g. "failed") is forwarded as a query param
        so the BFF's Inbox "Runs" tab can show only failed runs.
        """
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = str(limit)
        if agent_id is not None:
            params["agentId"] = agent_id
        if summary:
            params["summary"] = "true"  # upstream nulls the heavy columns (usage, log refs)
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
        min_count: Optional[int] = None,
    ) -> dict:
        """List currently-running heartbeat runs for the company.

        Maps to ``GET /api/companies/{companyId}/live-runs``. Used by the
        Inbox UI to show a pulsing "Live" badge on issues with active runs,
        and by the dashboard's Active-Agents strip.

        ``min_count`` is upstream's ``minCount`` padding floor — when the
        live-run set is short, upstream tops it up with recent runs so the
        strip isn't under-populated (mirrors the UI's ``MIN_DASHBOARD_RUNS``).
        """
        params = {"minCount": str(min_count)} if min_count is not None else None
        return await self._get(
            f"/api/companies/{company_id}/live-runs",
            session_cookie=session_cookie,
            params=params,
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

    async def get_run_log(
        self,
        *,
        session_cookie: str,
        run_id: str,
        offset: int = 0,
        limit_bytes: int = 65_536,
    ) -> dict:
        """Read a slice of a heartbeat run's raw log.

        Maps to ``GET /api/heartbeat-runs/{runId}/log``. Upstream returns
        ``{runId, store, logRef, content, nextOffset?}`` where ``content`` is
        the NDJSON-ish log slice from ``offset`` for up to ``limitBytes``
        bytes (upstream clamps to 1 MiB). The Dashboard live-run cards poll
        this for the in-progress agent transcript, reading a small tail by
        default (``limit_bytes`` ~ 64 KiB) so the card stays glanceable.
        """
        return await self._get(
            f"/api/heartbeat-runs/{run_id}/log",
            session_cookie=session_cookie,
            params={"offset": str(offset), "limitBytes": str(limit_bytes)},
        )

    async def get_run_events(
        self,
        *,
        session_cookie: str,
        run_id: str,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list | dict:
        """List a heartbeat run's structured event timeline.

        Maps to ``GET /api/heartbeat-runs/{runId}/events``. Upstream returns
        a raw JSON array of ``heartbeat_run_events`` rows (seq-ordered,
        payloads redacted server-side); ``afterSeq`` pages forward and
        ``limit`` is clamped upstream to 1..1000.
        """
        return await self._get(
            f"/api/heartbeat-runs/{run_id}/events",
            session_cookie=session_cookie,
            params={"afterSeq": str(after_seq), "limit": str(limit)},
        )

    async def cancel_heartbeat_run(
        self,
        *,
        session_cookie: str,
        run_id: str,
    ) -> dict | None:
        """Cancel a queued/running heartbeat run.

        Maps to ``POST /api/heartbeat-runs/{runId}/cancel`` (board-actor
        gated upstream; the BFF's per-user session cookie is a board actor).
        Upstream returns the cancelled run row, or JSON null when the run
        was already terminal.
        """
        return await self._post(
            f"/api/heartbeat-runs/{run_id}/cancel",
            session_cookie=session_cookie,
            json={},
        )

    async def record_watchdog_decision(
        self,
        *,
        session_cookie: str,
        run_id: str,
        body: dict,
    ) -> dict:
        """Record an operator decision on a silent/stalled run.

        Maps to ``POST /api/heartbeat-runs/{runId}/watchdog-decisions``.
        ``body`` is the camelCase wire dict (``decision``, optional
        ``evaluationIssueId`` / ``reason`` / ``snoozedUntil``); upstream
        validates the snooze-must-be-future rule and returns the inserted
        ``heartbeat_run_watchdog_decisions`` row.
        """
        return await self._post(
            f"/api/heartbeat-runs/{run_id}/watchdog-decisions",
            session_cookie=session_cookie,
            json=body,
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

    async def list_attention(
        self,
        *,
        session_cookie: str,
        company_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict:
        """Fetch the unified attention feed for the company.

        Maps to ``GET /api/companies/{companyId}/attention``. Returns an
        ``AttentionFeed`` object (NOT a bare array like the sibling list
        endpoints): ``{companyId, generatedAt, totalCount, deskBadgeCount,
        nextCursor, countsBySourceKind, items[]}``.

        ``totalCount`` / ``countsBySourceKind`` are computed upstream BEFORE
        pagination, so a small first page still carries the company-wide
        count — which is what makes them safe to render as a badge.
        """
        return await self._get(
            f"/api/companies/{company_id}/attention",
            session_cookie=session_cookie,
            params=params,
        )

    # ------------------------------------------------------------------
    # Inbox dismissals (per-user: always the caller's own session cookie)
    # ------------------------------------------------------------------

    async def list_inbox_dismissals(self, *, company_id: str, session_cookie: str) -> list | dict:
        """``GET /api/companies/{companyId}/inbox-dismissals`` — the caller's dismiss/snooze rows."""
        return await self._get(f"/api/companies/{company_id}/inbox-dismissals", session_cookie=session_cookie)

    async def create_inbox_dismissal(self, *, company_id: str, body: dict, session_cookie: str) -> dict:
        """``POST /api/companies/{companyId}/inbox-dismissals`` with ``{itemKey, kind?, snoozedUntil?}``."""
        return await self._post(
            f"/api/companies/{company_id}/inbox-dismissals", json=body, session_cookie=session_cookie
        )

    async def delete_inbox_dismissal(self, *, company_id: str, item_key: str, session_cookie: str) -> dict:
        """``DELETE /api/companies/{companyId}/inbox-dismissals/{itemKey}`` — restore an item.

        The key is percent-encoded whole (``safe=""``) so a ``/`` or ``?`` in it
        cannot rewrite the upstream path.
        """
        return await self._delete(
            f"/api/companies/{company_id}/inbox-dismissals/{quote(item_key, safe='')}",
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

    async def approve_approval(
        self,
        *,
        session_cookie: str,
        approval_id: str,
        note: Optional[str],
    ) -> dict:
        """Approve a pending approval, optionally with a reviewer note.

        Maps to ``POST /api/approvals/{approvalId}/approve``. Upstream's
        ``resolveApprovalSchema`` accepts only ``decisionNote`` (validators/
        approval.ts), so the BFF's ``note`` input is remapped to
        ``decisionNote`` here — sending ``note`` would be silently stripped
        and the reviewer's note lost. The body stays narrow (``decisionNote``
        only) to keep the ``payload.adapterType`` smuggling carrier closed.
        """
        body: dict[str, Any] = {"decisionNote": note} if note else {}
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

    async def list_approval_comments(self, *, session_cookie: str, approval_id: str) -> list | dict:
        """Maps to ``GET /api/approvals/{id}/comments``."""
        return await self._get(f"/api/approvals/{approval_id}/comments", session_cookie=session_cookie)

    async def add_approval_comment(self, *, session_cookie: str, approval_id: str, body: str) -> dict:
        """Maps to ``POST /api/approvals/{id}/comments`` (``{body}``)."""
        return await self._post(
            f"/api/approvals/{approval_id}/comments",
            json={"body": body},
            session_cookie=session_cookie,
        )

    async def request_approval_revision(
        self, *, session_cookie: str, approval_id: str, decision_note: Optional[str]
    ) -> dict:
        """Maps to ``POST /api/approvals/{id}/request-revision`` (board only upstream)."""
        body: dict[str, Any] = {"decisionNote": decision_note} if decision_note else {}
        return await self._post(
            f"/api/approvals/{approval_id}/request-revision",
            json=body,
            session_cookie=session_cookie,
        )

    async def resubmit_approval(self, *, session_cookie: str, approval_id: str) -> dict:
        """Maps to ``POST /api/approvals/{id}/resubmit`` with no replacement payload."""
        return await self._post(
            f"/api/approvals/{approval_id}/resubmit",
            json={},
            session_cookie=session_cookie,
        )

    async def list_approval_issues(self, *, session_cookie: str, approval_id: str) -> list | dict:
        """Maps to ``GET /api/approvals/{id}/issues`` (the linked tasks)."""
        return await self._get(f"/api/approvals/{approval_id}/issues", session_cookie=session_cookie)

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

    async def list_issue_approvals(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the approvals linked to an issue.

        Maps to ``GET /api/issues/{issueId}/approvals`` (issue-scoped).
        """
        return await self._get(
            f"/api/issues/{issue_id}/approvals",
            session_cookie=session_cookie,
        )

    async def list_issue_runs(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the agent runs for an issue.

        Maps to ``GET /api/issues/{issueId}/runs`` (issue-scoped).
        """
        return await self._get(
            f"/api/issues/{issue_id}/runs",
            session_cookie=session_cookie,
        )

    async def list_issue_documents(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the documents attached to an issue.

        Maps to ``GET /api/issues/{issueId}/documents`` (issue-scoped).
        The body is returned inline per document, so no fetch-by-key is needed.
        """
        return await self._get(
            f"/api/issues/{issue_id}/documents",
            session_cookie=session_cookie,
        )

    async def get_issue_document(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        key: str,
    ) -> dict:
        """Fetch a single issue document (with its body) by key.

        Maps to ``GET /api/issues/{issueId}/documents/{key}`` (issue-scoped;
        company asserted upstream via the session cookie). Returns the document
        object — ``{id, key, title, format, body, updatedAt, ...}`` — so the
        Artifacts viewer can render the markdown body in place instead of
        deep-linking into the issue detail.
        """
        return await self._get(
            f"/api/issues/{issue_id}/documents/{key}",
            session_cookie=session_cookie,
        )

    async def delete_issue_document(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        key: str,
    ) -> dict:
        """Delete an issue document by key.

        Maps to ``DELETE /api/issues/{issueId}/documents/{key}``. Upstream
        requires board authentication (the admin session is a board actor) and
        scopes the delete to the session cookie's company, so a cross-tenant
        issue id is rejected upstream. ``_delete`` treats 404 as already-gone,
        keeping a double-delete idempotent.
        """
        return await self._delete(
            f"/api/issues/{issue_id}/documents/{key}",
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

    async def upsert_issue_document(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        key: str,
        body: dict,
    ) -> tuple[int, dict]:
        """Create or replace an issue document body, minting a new revision.

        Maps to ``PUT /api/issues/{issueId}/documents/{key}`` (validated
        upstream against ``upsertIssueDocumentSchema``). Returns
        ``(status_code, document)`` — upstream answers 201 on create, 200 on
        update — so the BFF can forward that distinction.
        """
        return await self._put(
            f"/api/issues/{issue_id}/documents/{key}",
            json=body,
            session_cookie=session_cookie,
        )

    async def list_issue_document_revisions(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        key: str,
    ) -> list | dict:
        """List a document's revision history.

        Maps to ``GET /api/issues/{issueId}/documents/{key}/revisions``.
        Upstream returns a bare array of ``DocumentRevision`` rows (each with
        the full ``body`` so the diff modal can compare any two client-side).
        """
        return await self._get(
            f"/api/issues/{issue_id}/documents/{key}/revisions",
            session_cookie=session_cookie,
        )

    async def restore_issue_document_revision(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        key: str,
        revision_id: str,
    ) -> dict:
        """Restore a historical revision as a new latest revision.

        Maps to ``POST /api/issues/{issueId}/documents/{key}/revisions/
        {revisionId}/restore`` (empty body upstream). Returns the updated
        document.
        """
        return await self._post(
            f"/api/issues/{issue_id}/documents/{key}/revisions/{revision_id}/restore",
            json={},
            session_cookie=session_cookie,
        )

    async def list_document_annotations(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        key: str,
        status: Optional[str] = None,
        include_comments: bool = True,
    ) -> list | dict:
        """List annotation threads for a document.

        Maps to ``GET /api/issues/{issueId}/documents/{key}/annotations``.
        ``status`` filters open | resolved | all (upstream defaults to open);
        ``include_comments`` hydrates each thread's comment list. Upstream
        returns a bare array of ``DocumentAnnotationThreadWithComments``.
        """
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if include_comments:
            params["includeComments"] = "true"
        return await self._get(
            f"/api/issues/{issue_id}/documents/{key}/annotations",
            session_cookie=session_cookie,
            params=params or None,
        )

    async def create_document_annotation(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        key: str,
        body: dict,
    ) -> dict:
        """Create an annotation thread anchored to a text selection.

        Maps to ``POST /api/issues/{issueId}/documents/{key}/annotations``
        (validated upstream against ``createDocumentAnnotationThreadSchema``).
        Returns the created thread (with its first comment).
        """
        return await self._post(
            f"/api/issues/{issue_id}/documents/{key}/annotations",
            json=body,
            session_cookie=session_cookie,
        )

    async def add_document_annotation_comment(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        key: str,
        thread_id: str,
        body: dict,
    ) -> dict:
        """Append a reply comment to an annotation thread.

        Maps to ``POST /api/issues/{issueId}/documents/{key}/annotations/
        {threadId}/comments`` (validated upstream against
        ``createDocumentAnnotationCommentSchema``).
        """
        return await self._post(
            f"/api/issues/{issue_id}/documents/{key}/annotations/{thread_id}/comments",
            json=body,
            session_cookie=session_cookie,
        )

    async def update_document_annotation_thread(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        key: str,
        thread_id: str,
        body: dict,
    ) -> dict:
        """Update an annotation thread's status (resolve / reopen).

        Maps to ``PATCH /api/issues/{issueId}/documents/{key}/annotations/
        {threadId}`` (validated upstream against
        ``updateDocumentAnnotationThreadSchema``).
        """
        return await self._patch(
            f"/api/issues/{issue_id}/documents/{key}/annotations/{thread_id}",
            json=body,
            session_cookie=session_cookie,
        )

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

    async def list_issue_work_products(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the work-products produced for an issue.

        Maps to ``GET /api/issues/{issueId}/work-products`` (issue-scoped;
        ``paperclip/server/src/routes/issues.ts``). Upstream returns a bare
        array of ``IssueWorkProduct`` rows.
        """
        return await self._get(
            f"/api/issues/{issue_id}/work-products",
            session_cookie=session_cookie,
        )

    async def ensure_work_product_review_document(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        work_product_id: str,
    ) -> dict:
        """Materialize (or return) a Markdown work product's review document.

        Maps to ``POST /api/issues/{id}/work-products/{workProductId}/review-document``
        (``server/src/routes/issues.ts``). Upstream 404s a work product that is
        not on this issue/company; returns the ``artifact-review-<wpId>`` issue
        document (201 created, 200 existing).
        """
        return await self._post(
            f"/api/issues/{issue_id}/work-products/{work_product_id}/review-document",
            json={},
            session_cookie=session_cookie,
        )

    async def create_issue_work_product(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Create a work-product on an issue. Body is whitelisted by the BFF.

        Maps to ``POST /api/issues/{issueId}/work-products`` (validated
        upstream against ``createIssueWorkProductSchema``).
        """
        return await self._post(
            f"/api/issues/{issue_id}/work-products",
            json=body,
            session_cookie=session_cookie,
        )

    async def update_work_product(
        self,
        *,
        session_cookie: str,
        work_product_id: str,
        body: dict,
    ) -> dict:
        """Patch a work-product (the review-lifecycle transition).

        Maps to ``PATCH /api/work-products/{id}`` (validated upstream against
        ``updateIssueWorkProductSchema``, a partial). The board Approve /
        Request-changes actions send ``{reviewState}``. Company scope is
        asserted upstream against the session cookie's actor, so a
        cross-tenant work-product id is rejected upstream (404/403).
        """
        return await self._patch(
            f"/api/work-products/{work_product_id}",
            json=body,
            session_cookie=session_cookie,
        )

    async def delete_work_product(
        self,
        *,
        session_cookie: str,
        work_product_id: str,
    ) -> dict:
        """Delete a work-product.

        Maps to ``DELETE /api/work-products/{id}``. Upstream resolves the
        work-product, then asserts the session cookie's company owns the parent
        issue — so a cross-tenant id is rejected upstream (404/403) rather than
        relying on a BFF-side ownership check. ``_delete`` treats 404 as
        already-gone, keeping a double-delete idempotent.
        """
        return await self._delete(
            f"/api/work-products/{work_product_id}",
            session_cookie=session_cookie,
        )

    async def list_company_artifacts(
        self,
        *,
        session_cookie: str,
        company_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict:
        """List the company Artifacts gallery (documents + attachments +
        work-products, optionally grouped by task).

        Maps to ``GET /api/companies/{companyId}/artifacts`` — upstream
        ``assertCompanyAccess`` scopes it to the session cookie's company, so
        ``company_id`` MUST be the server-derived company of the caller (the
        BFF passes ``ctx.company_id``, never a client param). Returns the
        upstream ``{artifacts, groups?, selectedGroup?, nextCursor}`` object
        verbatim.
        """
        return await self._get(
            f"/api/companies/{company_id}/artifacts",
            session_cookie=session_cookie,
            params=params,
        )

    # ------------------------------------------------------------------
    # Company search (cross-entity: issues / comments / documents /
    # artifacts / agents / projects)
    # ------------------------------------------------------------------

    async def search_company(
        self,
        *,
        session_cookie: str,
        company_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict:
        """Ranked cross-entity company search.

        Maps to ``GET /api/companies/{companyId}/search`` — upstream
        ``assertCompanyAccess`` + a ``company_scope:read`` decision scope it
        to the session cookie's company, so ``company_id`` MUST be the
        server-derived company of the caller (the BFF passes ``ctx.company_id``,
        never a client param). ``params`` carries the query/scope/sort/filter
        pass-through (``q``, ``scope``, ``sort``, ``status``/``priority`` may
        repeat). Returns the upstream ``CompanySearchResponse`` object verbatim
        (``{results, countsByType, filterOptionCounts, zeroResults, hasMore, …}``).
        Upstream 429s the caller when the per-actor search rate limit is
        exceeded — that status is surfaced faithfully by the global
        ``PaperclipApiError`` handler.
        """
        return await self._get(
            f"/api/companies/{company_id}/search",
            session_cookie=session_cookie,
            params=params,
        )

    async def search_company_extract(
        self,
        *,
        session_cookie: str,
        company_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict:
        """Bulk literal/URL match extraction across company issues.

        Maps to ``GET /api/companies/{companyId}/search/extract`` — same
        company-scoping posture as :meth:`search_company` (``company_id`` is the
        server-derived caller company, never a client param). ``params`` carries
        ``contains`` (required, >=2 chars) plus ``kind``/``scope``/``limit``/
        ``offset``/``matchesPerIssue``/``status``/``updatedWithin``/
        ``updatedAfter``. Returns the upstream ``CompanySearchExtractResponse``
        verbatim.
        """
        return await self._get(
            f"/api/companies/{company_id}/search/extract",
            session_cookie=session_cookie,
            params=params,
        )

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

    async def list_issue_interactions(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the thread interactions for an issue.

        Maps to ``GET /api/issues/{issueId}/interactions`` (issue-scoped).
        Upstream returns a bare JSON array of interaction objects.
        """
        return await self._get(
            f"/api/issues/{issue_id}/interactions",
            session_cookie=session_cookie,
        )

    async def accept_interaction(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        interaction_id: str,
        body: dict,
    ) -> dict:
        """Accept a pending thread interaction.

        Maps to ``POST /api/issues/{issueId}/interactions/{interactionId}/accept``.
        Upstream's ``acceptIssueThreadInteractionSchema`` accepts optional
        ``selectedClientKeys`` / ``selectedOptionIds``; an empty ``{}`` accepts
        all/defaults. Board-only + company-scoped upstream (see class note).
        """
        return await self._post(
            f"/api/issues/{issue_id}/interactions/{interaction_id}/accept",
            json=body,
            session_cookie=session_cookie,
        )

    async def reject_interaction(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        interaction_id: str,
        body: dict,
    ) -> dict:
        """Reject a pending thread interaction.

        Maps to ``POST /api/issues/{issueId}/interactions/{interactionId}/reject``.
        Upstream's ``rejectIssueThreadInteractionSchema`` accepts an optional
        ``reason``. Board-only + company-scoped upstream (see class note).
        """
        return await self._post(
            f"/api/issues/{issue_id}/interactions/{interaction_id}/reject",
            json=body,
            session_cookie=session_cookie,
        )

    async def respond_interaction(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        interaction_id: str,
        body: dict,
    ) -> dict:
        """Respond to an ``ask_user_questions`` thread interaction.

        Maps to ``POST /api/issues/{issueId}/interactions/{interactionId}/respond``.
        Upstream's ``respondIssueThreadInteractionSchema`` requires ``answers``
        (an array of ``{questionId, optionIds, otherText?}``) plus an optional
        ``summaryMarkdown``. Board-only + company-scoped upstream (see class note).
        """
        return await self._post(
            f"/api/issues/{issue_id}/interactions/{interaction_id}/respond",
            json=body,
            session_cookie=session_cookie,
        )

    async def cancel_interaction(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        interaction_id: str,
        body: dict,
    ) -> dict:
        """Cancel an ``ask_user_questions`` thread interaction.

        Maps to ``POST /api/issues/{issueId}/interactions/{interactionId}/cancel``.
        Upstream's ``cancelIssueThreadInteractionSchema`` accepts an optional
        ``reason``. Board-only + company-scoped upstream (see class note).
        """
        return await self._post(
            f"/api/issues/{issue_id}/interactions/{interaction_id}/cancel",
            json=body,
            session_cookie=session_cookie,
        )

    async def skip_interaction(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        interaction_id: str,
        body: dict,
    ) -> dict:
        """Skip a pending thread interaction (status ``cancelled``, outcome
        ``skipped``). Unlike cancel, upstream enqueues NO continuation wake.

        Maps to ``POST /api/issues/{issueId}/interactions/{interactionId}/skip``.
        Board-only + company-scoped upstream (see class note).
        """
        return await self._post(
            f"/api/issues/{issue_id}/interactions/{interaction_id}/skip",
            json=body,
            session_cookie=session_cookie,
        )

    async def submit_interaction_verdicts(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        interaction_id: str,
        body: dict,
    ) -> dict:
        """Submit per-item verdicts on a ``request_item_verdicts`` interaction.

        Maps to ``POST /api/issues/{issueId}/interactions/{interactionId}/verdicts``.
        Wakes the assignee when any item newly resolves.
        """
        return await self._post(
            f"/api/issues/{issue_id}/interactions/{interaction_id}/verdicts",
            json=body,
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

    async def create_child_issue(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Create a child (sub-)issue under ``issue_id``. Body whitelisted by the BFF.

        Maps to ``POST /api/issues/{issueId}/children``.
        """
        return await self._post(
            f"/api/issues/{issue_id}/children",
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

    async def delete_issue(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Delete an issue.

        Maps to ``DELETE /api/issues/{issueId}``. ``_delete`` treats 404 as
        already-gone, so this is idempotent.
        """
        return await self._delete(
            f"/api/issues/{issue_id}",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Issue inbox state
    # ------------------------------------------------------------------

    async def archive_issue(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Archive an issue from the inbox.

        Maps to ``POST /api/issues/{id}/inbox-archive`` — the modern
        Paperclip flow that flips an issue's ``inbox_archived_at`` column.
        (Upstream's separate inbox-dismissals concept lives at
        ``POST /api/companies/{id}/inbox-dismissals``, keyed by ``itemKey``;
        see ``create_inbox_dismissal``.)
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

    async def monitor_check_now(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Trigger an immediate monitor re-check on an issue.

        Maps to ``POST /api/issues/{id}/monitor/check-now``. The monitor
        schedule itself lives on the issue's ``executionPolicy.monitor`` and
        is set via the issue PATCH; this endpoint re-runs the check now
        rather than waiting for ``nextCheckAt``.
        """
        return await self._post(
            f"/api/issues/{issue_id}/monitor/check-now",
            json={},
            session_cookie=session_cookie,
        )

    async def upsert_issue_watchdog(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Arm (or re-point) the issue's task watchdog. Body whitelisted by the
        BFF (``UpsertIssueWatchdogBody``).

        Maps to ``PUT /api/issues/{id}/watchdog`` → the watchdog. One per
        issue; upstream 409s when the named agent is not invokable. This is the
        task watchdog (a named agent reviewing a quiet subtree), not the
        run-silence watchdog on a run.
        """
        _status, watchdog = await self._put(
            f"/api/issues/{issue_id}/watchdog",
            json=body,
            session_cookie=session_cookie,
        )
        return watchdog

    async def delete_issue_watchdog(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Remove the issue's task watchdog.

        Maps to ``DELETE /api/issues/{id}/watchdog`` → ``{ok: true}``.
        """
        return await self._delete(
            f"/api/issues/{issue_id}/watchdog",
            session_cookie=session_cookie,
        )

    async def retry_scheduled_retry_now(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Promote the issue's parked scheduled retry so it runs now.

        Maps to ``POST /api/issues/{id}/scheduled-retry/retry-now`` (board-only
        upstream). Returns ``{outcome, message, scheduledRetry}``.
        """
        return await self._post(
            f"/api/issues/{issue_id}/scheduled-retry/retry-now",
            json={},
            session_cookie=session_cookie,
        )

    async def list_issue_recovery_actions(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Read an issue's recovery action. Maps to ``GET /api/issues/{id}/recovery-actions``
        (``{active, actions}``; upstream revalidates the active action on read)."""
        return await self._get(
            f"/api/issues/{issue_id}/recovery-actions",
            session_cookie=session_cookie,
        )

    async def resolve_issue_recovery_action(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Record the human's decision on a boarded recovery. Maps to
        ``POST /api/issues/{id}/recovery-actions/resolve`` (``{issue, recoveryAction}``)."""
        return await self._post(
            f"/api/issues/{issue_id}/recovery-actions/resolve",
            json=body,
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Company secrets. Board + company access upstream (``server/src/routes/
    # secrets.ts``); the one caller is ``paperclip_github_token``, as the
    # bootstrap admin. Values go IN only: no route here returns one.
    # ------------------------------------------------------------------

    async def list_company_secrets(self, *, session_cookie: str, company_id: str) -> list | dict:
        """Maps to ``GET /api/companies/{companyId}/secrets`` — a bare array of secret
        METADATA rows (``id``, ``name``, ``description``, ``status``, ...), deleted ones excluded."""
        return await self._get(
            f"/api/companies/{company_id}/secrets",
            session_cookie=session_cookie,
        )

    async def create_company_secret(
        self,
        *,
        session_cookie: str,
        company_id: str,
        name: str,
        value: str,
        description: str,
    ) -> dict:
        """Maps to ``POST /api/companies/{companyId}/secrets`` (Paperclip-managed, the
        instance's default provider). 409 when the name or its key already exists."""
        return await self._post(
            f"/api/companies/{company_id}/secrets",
            json={"name": name, "value": value, "description": description},
            session_cookie=session_cookie,
        )

    async def rotate_secret(self, *, session_cookie: str, secret_id: str, value: str) -> dict:
        """Maps to ``POST /api/secrets/{id}/rotate`` — writes a new version; the latest one
        is what resolvers read. (``PATCH /api/secrets/{id}`` takes no value.)"""
        return await self._post(
            f"/api/secrets/{secret_id}/rotate",
            json={"value": value},
            session_cookie=session_cookie,
        )

    async def update_secret_description(self, *, session_cookie: str, secret_id: str, description: str) -> dict:
        """Maps to ``PATCH /api/secrets/{id}`` with ``description`` alone (metadata only)."""
        return await self._patch(
            f"/api/secrets/{secret_id}",
            json={"description": description},
            session_cookie=session_cookie,
        )

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

    async def list_issue_external_objects(self, *, session_cookie: str, issue_id: str) -> list | dict:
        """Maps to ``GET /api/issues/{id}/external-objects`` — a bare array of
        ``{object, mentions, mentionCount, sourceLabels}`` groups."""
        return await self._get(
            f"/api/issues/{issue_id}/external-objects",
            session_cookie=session_cookie,
        )

    async def refresh_issue_external_objects(self, *, session_cookie: str, issue_id: str, body: dict) -> dict:
        """Maps to ``POST /api/issues/{id}/external-objects/refresh`` (``{refreshed}``)."""
        return await self._post(
            f"/api/issues/{issue_id}/external-objects/refresh",
            json=body,
            session_cookie=session_cookie,
        )

    async def list_external_object_summaries(
        self,
        *,
        session_cookie: str,
        company_id: str,
        issue_ids: list[str],
    ) -> dict:
        """Maps to ``POST /api/companies/{companyId}/issues/external-object-summaries``
        (``{summaries: {issueId: summary}}``). Upstream selects the requested ids
        ``WHERE company_id = :companyId``, so a foreign issue id yields no entry."""
        return await self._post(
            f"/api/companies/{company_id}/issues/external-object-summaries",
            json={"issueIds": issue_ids},
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
    # Issue comments
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

    async def delete_issue_comment(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        comment_id: str,
    ) -> dict:
        """Tombstone the caller's own comment. Maps to
        ``DELETE /api/issues/{id}/comments/{commentId}`` (upstream 403s a
        non-author; no ``?mode=cancel`` — queued-comment cancel is not exposed)."""
        return await self._delete(
            f"/api/issues/{issue_id}/comments/{comment_id}",
            session_cookie=session_cookie,
        )

    async def list_issue_live_runs(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """Queued/running runs for an issue. Maps to ``GET /api/issues/{id}/live-runs``."""
        return await self._get(
            f"/api/issues/{issue_id}/live-runs",
            session_cookie=session_cookie,
        )

    async def list_issue_activity(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """An issue's activity events, newest first. Maps to ``GET /api/issues/{id}/activity``."""
        return await self._get(
            f"/api/issues/{issue_id}/activity",
            session_cookie=session_cookie,
        )

    async def get_issue_active_run(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict | None:
        """The issue's active run or ``null``. Maps to ``GET /api/issues/{id}/active-run``."""
        return await self._get(
            f"/api/issues/{issue_id}/active-run",
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

    async def list_issue_feedback_votes(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the caller's feedback votes on an issue.

        Maps to ``GET /api/issues/{id}/feedback-votes`` — upstream returns a
        bare array of the current board user's votes on the issue's targets.
        """
        return await self._get(
            f"/api/issues/{issue_id}/feedback-votes",
            session_cookie=session_cookie,
        )

    async def upsert_issue_feedback_vote(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Cast (upsert) a feedback vote on an agent output. Body whitelisted
        by the BFF (``UpsertFeedbackVoteBody``).

        Maps to ``POST /api/issues/{id}/feedback-votes`` → 201 with the vote.
        """
        return await self._post(
            f"/api/issues/{issue_id}/feedback-votes",
            json=body,
            session_cookie=session_cookie,
        )

    async def list_issue_feedback_traces(
        self,
        *,
        session_cookie: str,
        company_id: str,
        issue_id: str,
    ) -> list | dict:
        """List feedback traces for an issue (shared-with-Labs export records).

        Upstream exposes traces ONLY on the COMPANY-scoped route
        ``GET /api/companies/{companyId}/feedback-traces`` with ``issueId`` as a
        query filter — there is no ``/api/issues/{id}/feedback-traces`` route
        (that path 404s). The BFF gates this on the company's
        ``feedbackDataSharingEnabled`` consent flag BEFORE calling — traces are
        not exposed when sharing is disabled.
        """
        return await self._get(
            f"/api/companies/{company_id}/feedback-traces",
            session_cookie=session_cookie,
            params={"issueId": issue_id},
        )

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

    async def list_decisions(
        self,
        *,
        session_cookie: str,
        company_id: str,
        status: str,
        limit: Optional[int] = None,
    ) -> list | dict:
        """List a company's decisions in one status, newest first (upstream
        defaults to 50 rows and caps ``limit`` at 100).

        Maps to ``GET /api/companies/{companyId}/decisions?status=`` — the
        only upstream read that annotates an open decision with
        ``targetChanged`` (which target issues drifted since it was proposed).
        """
        params: dict[str, Any] = {"status": status}
        if limit is not None:
            params["limit"] = limit
        return await self._get(
            f"/api/companies/{company_id}/decisions",
            session_cookie=session_cookie,
            params=params,
        )

    async def decide_decision(
        self,
        *,
        session_cookie: str,
        decision_id: str,
        body: dict,
    ) -> dict:
        """Pick an option on an open decision. Body whitelisted by the BFF
        (``DecideDecisionBody``).

        Maps to ``POST /api/decisions/{id}/decide`` → the decision plus its
        ``executions``.
        """
        return await self._post(
            f"/api/decisions/{decision_id}/decide",
            json=body,
            session_cookie=session_cookie,
        )

    async def dismiss_decision(
        self,
        *,
        session_cookie: str,
        decision_id: str,
        body: dict,
    ) -> dict:
        """Dismiss an open decision without running any effect.

        Maps to ``POST /api/decisions/{id}/dismiss``.
        """
        return await self._post(
            f"/api/decisions/{decision_id}/dismiss",
            json=body,
            session_cookie=session_cookie,
        )

    # (The feedback-traces consent gate reads ``feedbackDataSharingEnabled``
    # via the existing ``get_company`` method defined in the Company settings
    # section below.)

    # ------------------------------------------------------------------
    # Issue attachments
    # ------------------------------------------------------------------

    async def list_issue_attachments(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the binary attachments on an issue.

        Maps to ``GET /api/issues/{issueId}/attachments`` (issue-scoped;
        ``paperclip/server/src/routes/issues.ts``). Upstream returns a bare
        array of rows, each carrying the asset metadata plus a server-built
        ``contentPath`` (``/api/attachments/{id}/content``) and sibling
        ``openPath`` / ``downloadPath`` (see ``withContentPath``). Those paths
        are upstream-local, so the BFF rewrites them to its own
        ``/teams/issues/{issueId}/attachments/{id}/content`` proxy on the frontend.
        """
        return await self._get(
            f"/api/issues/{issue_id}/attachments",
            session_cookie=session_cookie,
        )

    async def upload_issue_attachment(
        self,
        *,
        session_cookie: str,
        company_id: str,
        issue_id: str,
        filename: str | None,
        content_type: str,
        body: bytes,
    ) -> dict:
        """Upload a file to an issue as multipart/form-data.

        Maps to ``POST /api/companies/{companyId}/issues/{issueId}/attachments``
        — the upstream route reads a SINGLE file from the ``"file"`` multipart
        field (``upload.single("file")``), so the field name is load-bearing.

        ``company_id`` MUST be the server-derived company of the caller (the
        BFF passes ``ctx.company_id``, never a client param) — this route is
        company-scoped and IDOR-sensitive.

        Returns the created attachment row (upstream ``withContentPath`` shape:
        the asset metadata + ``contentPath`` / ``openPath`` / ``downloadPath``).
        Bypasses ``_post`` because that helper forces a JSON body + JSON
        Content-Type; here httpx builds the multipart body (and its boundary
        Content-Type) from ``files=``, and ``_headers`` JSON Content-Type is
        dropped so it doesn't clobber the multipart boundary.
        """
        headers = self._headers(session_cookie)
        # httpx must own the Content-Type so it can append the multipart
        # boundary; leaving the JSON one set produces an unparseable body.
        headers.pop("Content-Type", None)
        resp = await self._send(
            lambda: self._http.post(
                f"/api/companies/{company_id}/issues/{issue_id}/attachments",
                files={"file": (filename or "attachment", body, content_type)},
                headers=headers,
            )
        )
        if resp.status_code >= 500:
            _emit_upstream_error("5xx")
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"POST /api/companies/{company_id}/issues/{issue_id}/attachments -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        return resp.json() if resp.content else {}

    async def open_attachment_stream(
        self,
        *,
        session_cookie: str,
        attachment_id: str,
    ) -> httpx.Response:
        """Open a streaming GET for an attachment's bytes and return the response.

        Maps to ``GET /api/attachments/{attachmentId}/content``. The response
        is opened with ``stream=True`` so its body is NOT buffered — the caller
        pipes ``resp.aiter_bytes()`` into a ``StreamingResponse`` and is
        responsible for calling ``resp.aclose()`` when the pump finishes (the
        BFF route does this in a ``finally``). Returning the open response
        (rather than yielding from a context manager) lets the route read the
        upstream headers up front and surface a >=400 as an HTTP error BEFORE
        the streaming body starts.

        Raises ``PaperclipApiError`` on a >=400 status, closing the response
        first so the connection isn't leaked on the error path.
        """
        req = self._http.build_request(
            "GET",
            f"/api/attachments/{attachment_id}/content",
            headers=self._headers(session_cookie),
        )
        resp = await self._send(lambda: self._http.send(req, stream=True))
        if resp.status_code >= 500:
            _emit_upstream_error("5xx")
        if resp.status_code >= 400:
            # Drain + close before raising so the error body is readable and
            # the streamed connection is released.
            await resp.aread()
            await resp.aclose()
            raise PaperclipApiError(
                f"GET /api/attachments/{attachment_id}/content -> {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        return resp

    async def delete_attachment(
        self,
        *,
        session_cookie: str,
        attachment_id: str,
    ) -> dict:
        """Delete an attachment.

        Maps to ``DELETE /api/attachments/{attachmentId}``. Upstream resolves
        the attachment, then runs ``assertCompanyAccess`` against the session
        cookie's company — so a cross-tenant id is rejected upstream (403)
        rather than relying on a BFF-side ownership check. ``_delete`` treats
        404 as already-gone, keeping a double-delete idempotent.
        """
        return await self._delete(
            f"/api/attachments/{attachment_id}",
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

    async def create_company_label(
        self,
        *,
        company_id: str,
        body: dict,
        session_cookie: str,
    ) -> dict:
        """Create a label on a Paperclip company.

        Maps to ``POST /api/companies/{companyId}/labels`` upstream
        (``createIssueLabelSchema``: ``{name, color}``, colour a required
        6-digit hex). Nothing seeds labels at provisioning, so without this
        every company's label set stays permanently empty and the rail's
        label picker has nothing to offer.
        """
        return await self._post(
            f"/api/companies/{company_id}/labels",
            json=body,
            session_cookie=session_cookie,
        )

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
        idempotency_key: str | None = None,
    ) -> dict:
        """Create a routine. Body is whitelisted by the BFF.

        Maps to ``POST /api/companies/{companyId}/routines``. With ``idempotency_key`` the
        fork replays the routine that key already made (200) instead of a duplicate.
        """
        return await self._post(
            f"/api/companies/{company_id}/routines",
            json=body,
            session_cookie=session_cookie,
            idempotency_key=idempotency_key,
        )

    async def create_routine_trigger(
        self,
        *,
        session_cookie: str,
        routine_id: str,
        body: dict,
    ) -> dict:
        """Attach a trigger (schedule/webhook/api) to a routine.

        Maps to ``POST /api/routines/{routineId}/triggers`` upstream
        (paperclip/server/src/routes/routines.ts). The cron schedule
        lives on a schedule trigger, NOT the routine row — see
        ``createRoutineTriggerSchema``. Body is whitelisted by the BFF
        (``CreateRoutineTriggerBody``). Returns ``{trigger, secretMaterial}``.
        """
        return await self._post(
            f"/api/routines/{routine_id}/triggers",
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
        """Remove a routine from service by archiving it.

        Upstream has NO ``DELETE /api/routines/{id}`` route — the old DELETE
        here always 404ed, ``_delete`` swallowed that as already-gone, and the
        "deleted" routine silently survived and kept firing. Archiving is the
        supported removal: ``tickScheduledTriggers`` only fires ``active``
        routines, and the archived row keeps its run history.

        Maps to ``PATCH /api/routines/{routineId}`` with status=archived.
        A 404 (routine already gone) is swallowed as success, preserving the
        idempotent-delete contract callers rely on for retries and
        double-deletes.
        """
        try:
            return await self._patch(
                f"/api/routines/{routine_id}",
                json={"status": "archived"},
                session_cookie=session_cookie,
            )
        except PaperclipApiError as exc:
            if exc.status_code == 404:
                return {}
            raise

    async def run_routine(
        self,
        *,
        routine_id: str,
        body: dict,
        session_cookie: str,
    ) -> dict:
        """Manually fire a routine.

        Maps to ``POST /api/routines/{routineId}/run`` upstream
        (paperclip/server/src/routes/routines.ts). Body is forwarded
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

    async def patch_routine_trigger(
        self,
        *,
        session_cookie: str,
        trigger_id: str,
        body: dict,
    ) -> dict:
        """Edit a routine trigger (schedule cron / webhook signing). Body is
        whitelisted by the BFF (``UpdateRoutineTriggerBody``).

        Maps to ``PATCH /api/routine-triggers/{triggerId}`` upstream
        (paperclip/server/src/routes/routines.ts).
        """
        return await self._patch(
            f"/api/routine-triggers/{trigger_id}",
            json=body,
            session_cookie=session_cookie,
        )

    async def delete_routine_trigger(
        self,
        *,
        session_cookie: str,
        trigger_id: str,
    ) -> dict:
        """Delete a routine trigger. 404 is swallowed as already-gone.

        Maps to ``DELETE /api/routine-triggers/{triggerId}`` upstream
        (paperclip/server/src/routes/routines.ts).
        """
        return await self._delete(
            f"/api/routine-triggers/{trigger_id}",
            session_cookie=session_cookie,
        )

    async def rotate_routine_trigger_secret(
        self,
        *,
        session_cookie: str,
        trigger_id: str,
        secret: str | None = None,
    ) -> dict:
        """Rotate a webhook trigger's signing secret. Returns the fresh
        ``{trigger, secretMaterial, ...}`` — the secret is shown to the
        operator exactly once.

        Maps to ``POST /api/routine-triggers/{triggerId}/rotate-secret``
        upstream (paperclip/server/src/routes/routines.ts,
        ``rotateRoutineTriggerSecretSchema``). ``secret=None`` rotates to a
        fresh RANDOM secret (the default). A ``secret`` value ARMS the trigger
        with that exact value — used to arm a provider-imposed webhook secret
        (a Stripe endpoint's ``whsec_``) so Paperclip verifies the provider's
        native signature against it. Board-only upstream (the bootstrap-admin
        session here is a board actor).
        """
        return await self._post(
            f"/api/routine-triggers/{trigger_id}/rotate-secret",
            json={} if secret is None else {"secret": secret},
            session_cookie=session_cookie,
        )

    async def list_routine_revisions(
        self,
        *,
        session_cookie: str,
        routine_id: str,
    ) -> list[dict]:
        """List a routine's revision history (title/trigger snapshots).

        Maps to ``GET /api/routines/{routineId}/revisions`` upstream
        (paperclip/server/src/routes/routines.ts). Returns a bare list.
        """
        result = await self._get(
            f"/api/routines/{routine_id}/revisions",
            session_cookie=session_cookie,
        )
        return result if isinstance(result, list) else []

    async def restore_routine_revision(
        self,
        *,
        session_cookie: str,
        routine_id: str,
        revision_id: str,
    ) -> dict:
        """Restore a routine to a prior revision snapshot.

        Maps to ``POST /api/routines/{routineId}/revisions/{revisionId}/restore``
        upstream (paperclip/server/src/routes/routines.ts). No body.
        """
        return await self._post(
            f"/api/routines/{routine_id}/revisions/{revision_id}/restore",
            json={},
            session_cookie=session_cookie,
        )

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

    async def get_goal(
        self,
        *,
        session_cookie: str,
        goal_id: str,
    ) -> dict:
        """Fetch a single goal by id (the goal detail page).

        Maps to ``GET /api/goals/{goalId}``. Upstream returns the full Goal
        row (id, companyId, title, description, level, status, parentId,
        ownerAgentId, createdAt, updatedAt) and 404s when the goal is gone or
        belongs to another company.
        """
        return await self._get(
            f"/api/goals/{goal_id}",
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

    async def preview_issue_tree_control(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Preview the blast radius of a subtree control action.

        Maps to ``POST /api/issues/{id}/tree-control/preview``. Returns the
        affected-issue/active-run totals + warnings without applying anything.
        """
        return await self._post(
            f"/api/issues/{issue_id}/tree-control/preview",
            json=body,
            session_cookie=session_cookie,
        )

    async def create_issue_tree_hold(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Create a subtree hold (pause / resume / cancel / restore).

        Maps to ``POST /api/issues/{id}/tree-holds``. Upstream interrupts
        active runs + defers wakeups for pause/cancel, and returns
        ``{hold, preview}``.
        """
        return await self._post(
            f"/api/issues/{issue_id}/tree-holds",
            json=body,
            session_cookie=session_cookie,
        )

    async def list_issue_tree_holds(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> list | dict:
        """List the subtree holds rooted at an issue.

        Maps to ``GET /api/issues/{id}/tree-holds`` (optional ``status`` /
        ``mode`` / ``includeMembers`` query filters).
        """
        return await self._get(
            f"/api/issues/{issue_id}/tree-holds",
            session_cookie=session_cookie,
            params=params,
        )

    async def get_issue_tree_hold(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        hold_id: str,
    ) -> dict:
        """Fetch a single subtree hold by id.

        Maps to ``GET /api/issues/{id}/tree-holds/{holdId}``.
        """
        return await self._get(
            f"/api/issues/{issue_id}/tree-holds/{hold_id}",
            session_cookie=session_cookie,
        )

    async def release_issue_tree_hold(
        self,
        *,
        session_cookie: str,
        issue_id: str,
        hold_id: str,
        body: dict,
    ) -> dict:
        """Release an active subtree hold.

        Maps to ``POST /api/issues/{id}/tree-holds/{holdId}/release``.
        """
        return await self._post(
            f"/api/issues/{issue_id}/tree-holds/{hold_id}/release",
            json=body,
            session_cookie=session_cookie,
        )

    async def get_issue_tree_control_state(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Fetch the active-pause-hold gate for an issue.

        Maps to ``GET /api/issues/{id}/tree-control/state``. Returns
        ``{activePauseHold: {...}|null}`` — non-null (with ``isRoot``) when
        this issue is under an active subtree pause hold.
        """
        return await self._get(
            f"/api/issues/{issue_id}/tree-control/state",
            session_cookie=session_cookie,
        )

    async def get_issue_subtree_diagnostics(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Read the subtree diagnostics for an issue.

        Maps to ``GET /api/issues/{id}/diagnostics/subtree`` — the read-only
        node/edge/readiness view that complements the pause/resume lever.
        """
        return await self._get(
            f"/api/issues/{issue_id}/diagnostics/subtree",
            session_cookie=session_cookie,
        )

    async def get_issue_blocker_diagnostics(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> dict:
        """Read the blocker diagnostics for an issue.

        Maps to ``GET /api/issues/{id}/diagnostics/blockers`` — why an issue
        is (or is not) blocked, complementing the subtree control view.
        """
        return await self._get(
            f"/api/issues/{issue_id}/diagnostics/blockers",
            session_cookie=session_cookie,
        )

    # ------------------------------------------------------------------
    # Cases (structured work-product records; upstream
    # ``server/src/routes/cases.ts``). Gated upstream by the instance flag
    # ``enableCases`` — while it is off every call answers 403 "Cases are
    # disabled". ``case_id`` may be the uuid or the ``LB-C7`` identifier.
    # Only the calls the Teams Cases panels make are wrapped here.
    # ------------------------------------------------------------------

    async def list_cases(
        self,
        *,
        session_cookie: str,
        company_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> list | dict:
        """List the company's cases.

        Maps to ``GET /api/companies/{companyId}/cases``. ``params`` is the
        upstream query (``types`` / ``status`` / ``statuses`` / ``projectIds`` /
        ``includeNoProject`` / ``labelId`` / ``parent`` / ``q`` /
        ``includeAncestors`` / ``limit``); list values repeat the key.
        Upstream returns a bare array.
        """
        return await self._get(
            f"/api/companies/{company_id}/cases",
            session_cookie=session_cookie,
            params=params,
        )

    async def get_case(self, *, session_cookie: str, case_id: str) -> dict:
        """Fetch one case with its labels, issue links, documents and attachments.

        Maps to ``GET /api/cases/{id}``.
        """
        return await self._get(f"/api/cases/{case_id}", session_cookie=session_cookie)

    async def patch_case(self, *, session_cookie: str, case_id: str, body: dict) -> dict:
        """Update a case (the board UI sends ``status`` and/or ``labelIds``).

        Maps to ``PATCH /api/cases/{id}``. Returns the full detail payload.
        """
        return await self._patch(f"/api/cases/{case_id}", json=body, session_cookie=session_cookie)

    async def list_case_events(self, *, session_cookie: str, case_id: str, limit: int = 100) -> list | dict:
        """List a case's event feed, newest first.

        Maps to ``GET /api/cases/{id}/events``. Upstream returns a bare array.
        """
        return await self._get(
            f"/api/cases/{case_id}/events",
            session_cookie=session_cookie,
            params={"limit": limit},
        )

    async def list_issue_cases(self, *, session_cookie: str, issue_id: str) -> list | dict:
        """List the cases linked to an issue (link role + case summary).

        Maps to ``GET /api/issues/{issueId}/cases``. Upstream returns a bare array.
        """
        return await self._get(f"/api/issues/{issue_id}/cases", session_cookie=session_cookie)

    async def list_case_document_revisions(self, *, session_cookie: str, case_id: str, key: str) -> dict:
        """List a case document's revision history.

        Maps to ``GET /api/cases/{id}/documents/{key}/revisions``. Unlike the
        issue route this returns ``{key, document, revisions: [...]}``.
        """
        return await self._get(
            f"/api/cases/{case_id}/documents/{key}/revisions",
            session_cookie=session_cookie,
        )

    async def restore_case_document_revision(
        self, *, session_cookie: str, case_id: str, key: str, revision_id: str
    ) -> dict:
        """Restore a historical revision as the new latest revision.

        Maps to ``POST /api/cases/{id}/documents/{key}/revisions/{revisionId}/restore``.
        """
        return await self._post(
            f"/api/cases/{case_id}/documents/{key}/revisions/{revision_id}/restore",
            json={},
            session_cookie=session_cookie,
        )

    async def list_case_document_annotations(
        self,
        *,
        session_cookie: str,
        case_id: str,
        key: str,
        status: Optional[str] = None,
        include_comments: bool = True,
    ) -> list | dict:
        """List annotation threads for a case document.

        Maps to ``GET /api/cases/{id}/documents/{key}/annotations``.
        """
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if include_comments:
            params["includeComments"] = "true"
        return await self._get(
            f"/api/cases/{case_id}/documents/{key}/annotations",
            session_cookie=session_cookie,
            params=params or None,
        )

    async def create_case_document_annotation(self, *, session_cookie: str, case_id: str, key: str, body: dict) -> dict:
        """Create an annotation thread on a case document.

        Maps to ``POST /api/cases/{id}/documents/{key}/annotations``.
        """
        return await self._post(
            f"/api/cases/{case_id}/documents/{key}/annotations",
            json=body,
            session_cookie=session_cookie,
        )

    async def add_case_document_annotation_comment(
        self, *, session_cookie: str, case_id: str, key: str, thread_id: str, body: dict
    ) -> dict:
        """Reply on a case document annotation thread.

        Maps to ``POST /api/cases/{id}/documents/{key}/annotations/{threadId}/comments``.
        """
        return await self._post(
            f"/api/cases/{case_id}/documents/{key}/annotations/{thread_id}/comments",
            json=body,
            session_cookie=session_cookie,
        )

    async def update_case_document_annotation_thread(
        self, *, session_cookie: str, case_id: str, key: str, thread_id: str, body: dict
    ) -> dict:
        """Resolve or reopen a case document annotation thread.

        Maps to ``PATCH /api/cases/{id}/documents/{key}/annotations/{threadId}``.
        """
        return await self._patch(
            f"/api/cases/{case_id}/documents/{key}/annotations/{thread_id}",
            json=body,
            session_cookie=session_cookie,
        )

    async def open_asset_stream(self, *, session_cookie: str, asset_id: str) -> httpx.Response:
        """Open a streaming GET for an asset's bytes (case attachments).

        Maps to ``GET /api/assets/{assetId}/content``. Same contract as
        ``open_attachment_stream``: the caller pipes ``aiter_bytes()`` and
        calls ``aclose()``; a >=400 raises ``PaperclipApiError`` after closing.
        """
        path = f"/api/assets/{asset_id}/content"
        req = self._http.build_request("GET", path, headers=self._headers(session_cookie))
        resp = await self._send(lambda: self._http.send(req, stream=True))
        if resp.status_code >= 500:
            _emit_upstream_error("5xx")
        if resp.status_code >= 400:
            await resp.aread()
            await resp.aclose()
            raise PaperclipApiError(f"GET {path} -> {resp.status_code}", status_code=resp.status_code, body=resp.text)
        return resp


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
