"""@audit_admin_action decorator — synchronous, fail-closed audit for /admin/* writes.

Per CEO review S1 (issue #351):
- Audit row is written BEFORE the response is returned (synchronous).
- If DDB write fails, the primary action's side effect has already
  executed (Stripe/ECS/etc. — can't be rolled back), so:
    * a CRITICAL log entry "ADMIN_AUDIT_PANIC" is emitted with full context,
    * the response is annotated audit_status="panic" so the UI can warn.
- The decorator never raises from its own audit logic; it only re-raises
  exceptions originating in the wrapped handler.

Usage:

    @router.post("/admin/users/{user_id}/container/reprovision")
    @audit_admin_action("container.reprovision")
    async def handler(
        user_id: str,
        request: Request,
        auth: AuthContext = Depends(require_platform_admin),
    ):
        ...

The wrapped handler MUST take `request: Request` and `auth: AuthContext`
as kwargs (or args). The target user id comes from a path param named
`user_id` by default; override via `target_param="owner_id"` etc.

For write endpoints with a request body, also pass `body=<pydantic model>`
so the decorator can capture and (optionally) redact it for the audit row.
"""

import functools
import logging
import time
from typing import Any, Callable

from core.auth import AuthContext
from core.repositories import admin_actions_repo

logger = logging.getLogger(__name__)


def _extract_client_ip(request: Any) -> str:
    """First X-Forwarded-For hop (set by Vercel's edge), or client.host."""
    if request is None:
        return "unknown"
    xff = request.headers.get("x-forwarded-for", "") if hasattr(request, "headers") else ""
    if xff:
        return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "unknown") if client else "unknown"


def _extract_user_agent(request: Any) -> str:
    if request is None or not hasattr(request, "headers"):
        return "unknown"
    return request.headers.get("user-agent", "unknown")


def _redact_payload(payload: dict, redact_paths: list[str]) -> dict:
    """Shallow redaction — replaces top-level keys in redact_paths with '***redacted***'.

    For deep redaction of openclaw.json secret fields, see
    core.services.admin_redact.redact_openclaw_config.
    """
    if not redact_paths:
        return payload
    return {k: ("***redacted***" if k in redact_paths else v) for k, v in payload.items()}


def _payload_from_body(body: Any) -> dict:
    if body is None:
        return {}
    if hasattr(body, "model_dump"):
        return body.model_dump()
    if isinstance(body, dict):
        return body
    return {}


def _audit_value(v: Any) -> Any:
    """Coerce a kwarg into a JSON-friendly shape for the audit payload."""
    if hasattr(v, "model_dump"):
        try:
            return v.model_dump()
        except Exception:
            return str(v)
    return v


def _payload_from_capture_params(kwargs: dict, capture_params: list[str]) -> dict:
    """Build the audit payload from named kwargs.

    Each listed name becomes a top-level key. Pydantic models are serialized
    via ``model_dump()``; path/query params pass through as-is.
    """
    return {k: _audit_value(kwargs.get(k)) for k in capture_params}


def _find_auth(args: tuple, kwargs: dict) -> AuthContext | None:
    auth = kwargs.get("auth")
    if isinstance(auth, AuthContext):
        return auth
    for a in args:
        if isinstance(a, AuthContext):
            return a
    return None


def audit_admin_action(
    action: str,
    *,
    target_param: str = "user_id",
    target_user_id_override: str | None = None,
    redact_paths: list[str] | None = None,
    capture_params: list[str] | None = None,
) -> Callable:
    """Decorate an admin router handler to write an audit row per call.

    See module docstring for the contract. `action` is a dotted name like
    'container.reprovision' that ends up in the audit_actions DDB row's
    `action` field (the same value the audit viewer uses for filtering).

    `target_user_id_override`: when set (e.g. "__catalog__"), the audit
    row's `target_user_id` is this static value rather than being pulled
    from the handler's kwargs. Intended for actions that operate on a
    shared resource (the catalog, platform config, etc.) rather than a
    specific user.

    `capture_params`: explicit list of handler kwarg names to include in
    the audit `payload`. Pydantic models are serialized via
    ``model_dump()``; other values pass through unchanged. Use this when
    the request body arg is named something other than `body`, or when
    the meaningful identifier is a path/query param (e.g. `slug`). When
    omitted, the decorator falls back to its historical behavior of
    serializing the `body` kwarg.
    """
    redact_paths = redact_paths or []

    def decorator(handler: Callable) -> Callable:
        @functools.wraps(handler)
        async def wrapped(*args, **kwargs):
            auth = _find_auth(args, kwargs)
            if auth is None:
                # Programming error — decorator misused. Surface loudly.
                raise RuntimeError(f"@audit_admin_action({action!r}) requires AuthContext in args or kwargs")

            request = kwargs.get("request")
            if target_user_id_override is not None:
                target_user_id = target_user_id_override
            else:
                target_user_id = kwargs.get(target_param) or "system"
            if capture_params:
                raw_payload = _payload_from_capture_params(kwargs, capture_params)
            else:
                raw_payload = _payload_from_body(kwargs.get("body"))
            payload = _redact_payload(raw_payload, redact_paths)

            user_agent = _extract_user_agent(request)
            ip = _extract_client_ip(request)

            started = time.monotonic()
            result_label = "success"
            http_status = 200
            error_message: str | None = None
            handler_result: Any = None
            handler_exc: BaseException | None = None

            try:
                handler_result = await handler(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 — re-raised after audit
                handler_exc = e
                result_label = "error"
                http_status = getattr(e, "status_code", 500)
                error_message = getattr(e, "detail", None) or str(e)

            elapsed_ms = int((time.monotonic() - started) * 1000)

            audit_status = "written"
            try:
                await admin_actions_repo.create(
                    admin_user_id=auth.user_id,
                    target_user_id=target_user_id,
                    action=action,
                    payload=payload,
                    result=result_label,
                    audit_status="written",
                    http_status=http_status,
                    elapsed_ms=elapsed_ms,
                    error_message=error_message,
                    user_agent=user_agent,
                    ip=ip,
                )
            except Exception as audit_exc:  # noqa: BLE001
                # CEO S1 fail-closed: action already executed; surface the
                # gap loudly but don't double-fail the request.
                audit_status = "panic"
                logger.critical(
                    "ADMIN_AUDIT_PANIC action=%s admin=%s target=%s err=%s",
                    action,
                    auth.user_id,
                    target_user_id,
                    audit_exc,
                    extra={
                        "admin_action": action,
                        "admin_user_id": auth.user_id,
                        "target_user_id": target_user_id,
                        "result": result_label,
                        "http_status": http_status,
                        "audit_error": str(audit_exc),
                    },
                )

            if handler_exc is not None:
                raise handler_exc

            # Tag dict responses with audit_status so the UI can warn the operator
            # when a write succeeded but the audit row didn't land.
            if isinstance(handler_result, dict):
                return {**handler_result, "audit_status": audit_status}
            return handler_result

        return wrapped

    return decorator
