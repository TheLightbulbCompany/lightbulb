"""Runtime discovery of the per-env bedrock-gate Function URL.

The gateway is the STANDARD Bedrock path: every Bedrock provision points the
container's ``amazon-bedrock`` provider (and the ACP harness) at the gate. The
URL is minted at deploy time by the bedrock-gate CDK stack and published to SSM
at ``/lightbulb/{env}/bedrock-gate-url`` — read here at provision time (cached), so
no deploy-ordering dance and no cross-stack env threading.

Resolution order:
  1. ``settings.BEDROCK_GATE_URL`` — explicit override (tests / local dev).
  2. The SSM parameter, cached for ``_CACHE_TTL_S``.

Returns ``""`` (route DIRECT to bedrock-runtime) when the parameter is missing
(bootstrap: the gate stack hasn't deployed yet) or holds the literal value
``"direct"`` — the fleet BREAK-GLASS lever (SSM forbids empty strings):

    aws ssm put-parameter --name /lightbulb/{env}/bedrock-gate-url \\
        --value direct --overwrite

NOTE: post gateway-cutover the per-owner roles carry NO ``bedrock:Invoke``, so
"direct" alone produces AccessDenied — the break-glass runbook pairs it with
``scripts/bedrock_gate_break_glass.py`` which re-grants direct IAM. See
``docs/runbooks/bedrock-gate-break-glass.md``.
"""

from __future__ import annotations

import logging
import time

import boto3

from core.config import settings

logger = logging.getLogger(__name__)

_PARAM_NAME_TMPL = "/lightbulb/{env}/bedrock-gate-url"
_CACHE_TTL_S = 60

_cache: tuple[float, str] | None = None


def reset_cache() -> None:
    """Test seam — drop the cached value so the next call re-reads SSM."""
    global _cache
    _cache = None


class GateUrlUnavailable(RuntimeError):
    """The bedrock-gate URL parameter is missing or unreadable.

    Deliberately a HARD provisioning error, not a silent fallback: the gateway
    is the standard Bedrock path, and a config quietly written with the direct
    endpoint would AccessDeny at inference time (owner roles carry no
    bedrock:Invoke) — a far more confusing failure than this one. The ONLY
    sanctioned direct mode is the explicit break-glass parameter value
    ``"direct"`` (paired with the IAM re-grant script — see the runbook).
    """


def gate_url() -> str:
    """The env's bedrock-gate URL, or ``""`` iff break-glass ``"direct"`` mode.

    Synchronous by design: callers are provision-time config builders (already
    seconds-long paths), the value is cached, and a cold read is one SSM
    GetParameter (~10ms).

    Raises:
        GateUrlUnavailable: the parameter is missing or unreadable — fail
            provisioning loudly at the misconfiguration, never route silently.
    """
    global _cache
    if settings.BEDROCK_GATE_URL:
        return settings.BEDROCK_GATE_URL
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_S:
        return _cache[1]
    env = settings.ENVIRONMENT or "dev"
    name = _PARAM_NAME_TMPL.format(env=env)
    try:
        resp = boto3.client("ssm", region_name=settings.AWS_REGION).get_parameter(Name=name)
    except Exception as exc:
        raise GateUrlUnavailable(
            f"bedrock-gate URL parameter {name} is missing/unreadable ({exc}). "
            "Deploy the bedrock-gate stack (it publishes the parameter), or for "
            "an emergency set its value to the literal 'direct' AND run the "
            "break-glass IAM re-grant — see docs/runbooks/bedrock-gate-break-glass.md."
        ) from exc
    val = (resp["Parameter"]["Value"] or "").strip()
    if val.lower() == "direct":
        logger.warning("bedrock-gate parameter %s is in BREAK-GLASS 'direct' mode", name)
        val = ""
    _cache = (now, val)
    return val
