"""Stable logical-operation keys for Paperclip agent creation.

The key intentionally excludes adapter credentials. Lightbulb remints the
service token and operator device key for every attempt, so hashing the raw
Paperclip request would turn an honest retry into a distinct create.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_OPERATION_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_KEY_PREFIX = "lightbulb-agent-create:v1"


def build_agent_create_idempotency_key(
    *,
    operation: str,
    owner_id: str,
    intent: Mapping[str, Any],
) -> str:
    """Return a bounded key for one owner-scoped logical create operation.

    ``intent`` must contain only stable business inputs. Never pass an adapter
    config, service token, operator private key, request id, or actor id.

    For the unanchored chat and Teams create paths, identical semantic inputs
    deliberately identify the same active-agent intent. Paperclip's mapping is
    deleted with the agent, so deleting that agent permits the same intent to
    create a fresh identity later. Anchored workflows (marketplace and Atlas)
    include their durable listing, issue, or staging identity in ``intent``.
    """

    if not _OPERATION_RE.fullmatch(operation):
        raise ValueError("operation must be a lowercase kebab-case identifier")
    if not owner_id:
        raise ValueError("owner_id is required")

    canonical = json.dumps(
        {
            "intent": intent,
            "operation": operation,
            "ownerId": owner_id,
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"{_KEY_PREFIX}:{operation}:{digest}"


def paperclip_agent_intent(
    *,
    name: str,
    role: str,
    adapter_type: str,
    title: str | None = None,
    capabilities: str | None = None,
    icon: str | None = None,
    reports_to: str | None = None,
    budget_monthly_cents: int = 0,
) -> dict[str, Any]:
    """Build the credential-free semantic subset of a create-agent body."""

    intent: dict[str, Any] = {
        "adapterType": adapter_type,
        "name": name,
        "role": role,
    }
    if title is not None:
        intent["title"] = title
    if capabilities is not None:
        intent["capabilities"] = capabilities
    if icon is not None:
        intent["icon"] = icon
    if reports_to is not None:
        intent["reportsTo"] = reports_to
    if budget_monthly_cents:
        intent["budgetMonthlyCents"] = budget_monthly_cents
    return intent
