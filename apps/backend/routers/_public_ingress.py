"""The caller-IP read shared by the unauthenticated public routes."""

from fastapi import Request


def _extract_caller_ip(request: Request) -> str:
    """Get the immediate TCP-peer IP, ignoring inbound X-Forwarded-* (untrusted).

    In production this is the ALB's private IP. When uvicorn is configured with
    ``forwarded_allow_ips`` covering the ALB's subnet, starlette already surfaces the
    trusted rightmost XFF entry (= the real client) as ``request.client.host`` — so this
    returns the real-client IP without trusting user-controlled headers.
    """
    return request.client.host if request.client else "0.0.0.0"
