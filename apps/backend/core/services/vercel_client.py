"""Thin Vercel REST client for the company's own Vercel project.

``creds`` is the revealed credential dict (``token``, ``org_id`` -> ``teamId``, ``project_id``).
One request per call, no waiting: callers poll ``deployment_state``. Never logs or raises with the
token, headers or env values.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

API = "https://api.vercel.com"
TIMEOUT_S = 30

# Tests swap in an httpx.MockTransport; production leaves it None.
_transport: httpx.AsyncBaseTransport | None = None


class VercelError(Exception):
    """Safe to show: never carries the token. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, timeout=TIMEOUT_S)


async def _call(
    creds: dict,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: Any = None,
    content: bytes | None = None,
    headers: dict | None = None,
) -> Any:
    if not all(creds.get(k) for k in ("token", "org_id", "project_id")):
        raise VercelError("missing_credentials", "Vercel credentials are incomplete.")
    h = {"Authorization": f"Bearer {creds['token']}", **(headers or {})}
    try:
        async with _client() as c:
            r = await c.request(
                method,
                API + path,
                params={**(params or {}), "teamId": creds["org_id"]},
                json=json,
                content=content,
                headers=h,
            )
    except httpx.HTTPError as e:
        raise VercelError("network", f"Vercel request failed ({type(e).__name__}).") from None
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 300:
        err = data.get("error") if isinstance(data, dict) else None
        err = err if isinstance(err, dict) else {}
        msg = str(err.get("message") or f"Vercel returned {r.status_code}").replace(creds["token"], "***")
        raise VercelError(str(err.get("code") or f"http_{r.status_code}"), msg)
    return data


def _pid(creds: dict) -> str:
    return creds.get("project_id", "")


async def upsert_env(creds: dict, env: dict[str, str], targets: list[str]) -> None:
    items = [{"key": k, "value": v, "type": "encrypted", "target": targets} for k, v in env.items()]
    await _call(creds, "POST", f"/v10/projects/{_pid(creds)}/env", params={"upsert": "true"}, json=items)


async def upload_file(creds: dict, data: bytes) -> str:
    sha = hashlib.sha1(data).hexdigest()  # noqa: S324 - Vercel's file digest is sha1
    await _call(
        creds,
        "POST",
        "/v2/files",
        content=data,
        headers={"x-vercel-digest": sha, "Content-Type": "application/octet-stream"},
    )
    return sha


async def create_deployment(
    creds: dict, *, name: str, files: list[dict], target: str | None, build_command: str
) -> dict:
    body = {
        "name": name,
        "project": _pid(creds),
        "files": files,
        "target": target,
        "projectSettings": {"framework": "nextjs", "buildCommand": build_command},
    }
    d = await _call(
        creds, "POST", "/v13/deployments", params={"forceNew": "1", "skipAutoDetectionConfirmation": "1"}, json=body
    )
    return {k: d.get(k) for k in ("id", "url", "readyState")}


async def deployment_state(creds: dict, deployment_id: str) -> dict:
    d = await _call(creds, "GET", f"/v13/deployments/{deployment_id}")
    return {"readyState": d.get("readyState"), "url": d.get("url"), "errorMessage": d.get("errorMessage")}


async def deployment_events(creds: dict, deployment_id: str, *, limit: int = 50) -> list[dict]:
    evs = await _call(
        creds, "GET", f"/v3/deployments/{deployment_id}/events", params={"limit": limit, "direction": "forward"}
    )
    return [
        {"at": e.get("created"), "message": e.get("text") or (e.get("payload") or {}).get("text") or ""}
        for e in (evs if isinstance(evs, list) else [])
    ]


async def add_domain(creds: dict, name: str) -> dict:
    d = await _call(creds, "POST", f"/v10/projects/{_pid(creds)}/domains", json={"name": name})
    info = await _call(creds, "GET", f"/v9/projects/{_pid(creds)}/domains/{name}")
    return {"verified": d.get("verified"), "verification": d.get("verification", []), "cname": info.get("cname")}


async def verify_domain(creds: dict, name: str) -> bool:
    d = await _call(creds, "POST", f"/v9/projects/{_pid(creds)}/domains/{name}/verify")
    return bool(d.get("verified"))


async def rollback(creds: dict, deployment_id: str) -> None:
    await _call(creds, "POST", f"/v1/projects/{_pid(creds)}/rollback/{deployment_id}")


async def disable_protection(creds: dict) -> None:
    await _call(creds, "PATCH", f"/v9/projects/{_pid(creds)}", json={"ssoProtection": None, "passwordProtection": None})


async def delete_project(creds: dict) -> None:
    await _call(creds, "DELETE", f"/v9/projects/{_pid(creds)}")
