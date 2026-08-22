"""Public read-only Paperclip board projection for Isol8.

This route is deliberately a projection, not a proxy. It exposes only the fields
approved for the public company board and never forwards arbitrary Paperclip
payloads, comments, documents, credentials, prompts, adapter config, or write
capability.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Response

router = APIRouter()

DEFAULT_COMPANY_ID = "1071914e-6b9a-4351-a1d6-58bffe2e30f1"
PUBLIC_STATUSES = "todo,in_progress,in_review,blocked,done"
MAX_ISSUES = 100
MAX_RUNS = 80
MAX_ROUTINES = 100
MAX_WORK_PRODUCTS_PER_ISSUE = 12


def _enabled() -> bool:
    return os.getenv("PAPERCLIP_PUBLIC_BOARD_ENABLED", "").lower() in {"1", "true", "yes"}


def _api_base() -> str:
    return os.getenv("PAPERCLIP_PUBLIC_BOARD_API_URL") or os.getenv(
        "PAPERCLIP_API_URL", "http://paperclip.isol8-prod.local:3100/"
    )


def _api_key() -> str:
    return os.getenv("PAPERCLIP_PUBLIC_BOARD_API_KEY") or os.getenv("PAPERCLIP_API_KEY", "")


def _company_id() -> str:
    return os.getenv("PAPERCLIP_PUBLIC_BOARD_COMPANY_ID") or os.getenv("PAPERCLIP_COMPANY_ID") or DEFAULT_COMPANY_ID


def _public_url() -> str:
    frontend = os.getenv("FRONTEND_URL", "https://isol8.co").rstrip("/")
    return os.getenv("PAPERCLIP_PUBLIC_BOARD_URL") or f"{frontend}/company-board"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _pick(obj: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: obj.get(key) for key in fields if obj.get(key) is not None}


def _safe_link(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    if not (url.startswith("https://") or url.startswith("http://")):
        return None
    # Public work products must be externally inspectable URLs. Internal/private
    # transport URLs and local paths stay hidden even if upstream stored them.
    if any(token in url.lower() for token in ("localhost", "127.0.0.1", ".local", "internal", "secret", "token=")):
        return None
    return url


def _list_from(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        value = payload.get(key) or payload.get("items") or payload.get("data")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _project_work_product(item: dict[str, Any]) -> dict[str, Any] | None:
    link = _safe_link(item.get("url") or item.get("externalUrl") or item.get("href"))
    projected = _pick(
        item,
        (
            "id",
            "title",
            "type",
            "kind",
            "status",
            "createdAt",
            "updatedAt",
            "reviewStatus",
        ),
    )
    if link:
        projected["url"] = link
    if not projected.get("url") and not projected.get("title"):
        return None
    return projected


def _project_issue(item: dict[str, Any], work_products: list[dict[str, Any]]) -> dict[str, Any]:
    projected = _pick(
        item,
        (
            "id",
            "identifier",
            "title",
            "status",
            "priority",
            "workMode",
            "createdAt",
            "updatedAt",
            "startedAt",
            "completedAt",
            "assigneeAgentId",
            "responsibleUserId",
            "executionRunId",
            "parentId",
        ),
    )
    labels = item.get("labels")
    if isinstance(labels, list):
        projected["labels"] = [_pick(label, ("id", "name", "color")) for label in labels if isinstance(label, dict)]
    projected["workProducts"] = work_products
    return projected


def _project_routine(item: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        item,
        (
            "id",
            "identifier",
            "title",
            "status",
            "priority",
            "enabled",
            "createdAt",
            "updatedAt",
            "assigneeAgentId",
            "reviewerAgentId",
            "reviewerUserId",
            "lastRunAt",
            "nextRunAt",
        ),
    )


def _project_run(item: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        item,
        (
            "id",
            "issueId",
            "routineId",
            "agentId",
            "status",
            "reason",
            "startedAt",
            "finishedAt",
            "createdAt",
            "updatedAt",
        ),
    )


async def _fetch_json(client: httpx.AsyncClient, path: str, *, params: dict[str, Any] | None = None) -> Any:
    resp = await client.get(path, params=params)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="public board upstream read failed")
    return resp.json()


@router.get("/board")
async def get_public_board(response: Response) -> dict[str, Any]:
    if not _enabled():
        raise HTTPException(status_code=404, detail="public board not published")
    token = _api_key()
    if not token:
        raise HTTPException(status_code=503, detail="public board source is not configured")

    company_id = _company_id()
    headers = {"Authorization": f"Bearer {token}"}
    base_url = _api_base().rstrip("/") + "/"
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=10) as client:
        issues_raw = _list_from(
            await _fetch_json(
                client,
                f"/api/companies/{company_id}/issues",
                params={"status": PUBLIC_STATUSES, "limit": str(MAX_ISSUES)},
            ),
            "issues",
        )
        routines_raw = _list_from(
            await _fetch_json(client, f"/api/companies/{company_id}/routines"),
            "routines",
        )[:MAX_ROUTINES]
        runs_raw = _list_from(
            await _fetch_json(
                client,
                f"/api/companies/{company_id}/heartbeat-runs",
                params={"limit": str(MAX_RUNS)},
            ),
            "runs",
        )

        issues: list[dict[str, Any]] = []
        for issue in issues_raw[:MAX_ISSUES]:
            issue_id = issue.get("id")
            work_products: list[dict[str, Any]] = []
            if isinstance(issue_id, str):
                products_raw = _list_from(
                    await _fetch_json(client, f"/api/issues/{issue_id}/work-products"),
                    "workProducts",
                )
                work_products = [
                    product
                    for product in (_project_work_product(item) for item in products_raw[:MAX_WORK_PRODUCTS_PER_ISSUE])
                    if product is not None
                ]
            issues.append(_project_issue(issue, work_products))

    response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=120"
    return {
        "title": "Live Isol8 company board",
        "companyId": company_id,
        "publicUrl": _public_url(),
        "generatedAt": _now_iso(),
        "disclosureBoundary": {
            "included": ["issues", "routines", "heartbeat runs", "reviewer ids", "public work-product links"],
            "excluded": ["comments", "private documents", "attachments", "secrets", "customer data", "write endpoints"],
        },
        "issues": issues,
        "routines": [_project_routine(item) for item in routines_raw],
        "runs": [_project_run(item) for item in runs_raw],
    }
