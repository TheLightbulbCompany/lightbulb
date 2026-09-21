"""Discover the HTTPS base URL for the managed OpenRouter inference gate."""

from __future__ import annotations

import time

import boto3

from core.config import settings


class GateUrlUnavailable(RuntimeError):
    pass


_cache: tuple[float, str] | None = None


def reset_cache() -> None:
    global _cache
    _cache = None


def gate_url() -> str:
    global _cache
    if settings.OPENROUTER_GATE_URL:
        raw = settings.OPENROUTER_GATE_URL
    elif _cache and time.monotonic() - _cache[0] < 60:
        return _cache[1]
    else:
        name = f"/lightbulb/{settings.ENVIRONMENT or 'dev'}/openrouter-gate-url"
        try:
            raw = boto3.client("ssm", region_name=settings.AWS_REGION).get_parameter(Name=name)["Parameter"]["Value"]
        except Exception as exc:
            raise GateUrlUnavailable(f"managed OpenRouter gate URL {name} is unavailable") from exc
    url = raw.rstrip("/")
    if not url.startswith("https://"):
        raise GateUrlUnavailable("managed OpenRouter gate URL must be HTTPS")
    url = url if url.endswith("/v1") else f"{url}/v1"
    _cache = (time.monotonic(), url)
    return url
