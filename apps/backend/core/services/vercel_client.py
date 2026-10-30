"""Thin Vercel REST client for the company's own Vercel project.

``creds`` is the revealed credential dict (``token``, ``org_id`` -> ``teamId``, ``project_id``).
One request per call, no waiting: callers poll ``deployment_state``. Never logs or raises with the
token, headers or env values.
"""

from __future__ import annotations

import asyncio
import hashlib
from json import loads as json_loads
from typing import Any
from urllib.parse import quote

import httpx

API = "https://api.vercel.com"
TIMEOUT_S = 30

# Tests swap in an httpx.MockTransport; production leaves it None.
_transport: httpx.AsyncBaseTransport | None = None


class VercelError(Exception):
    """Safe to show: never carries the token. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, timeout=TIMEOUT_S)


def _headers(creds: dict, extra: dict | None = None) -> dict:
    if not all(creds.get(k) for k in ("token", "org_id", "project_id")):
        raise VercelError("missing_credentials", "Vercel credentials are incomplete.")
    return {"Authorization": f"Bearer {creds['token']}", **(extra or {})}


def _raise(creds: dict, status: int, data: Any) -> None:
    err = data.get("error") if isinstance(data, dict) else None
    err = err if isinstance(err, dict) else {}
    msg = str(err.get("message") or f"Vercel returned {status}").replace(creds["token"], "***")
    raise VercelError(str(err.get("code") or f"http_{status}"), msg, status)


async def _call(
    creds: dict,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: Any = None,
    content: bytes | None = None,
    headers: dict | None = None,
    expect: Any = dict,
) -> Any:
    """One request. ``expect``: a type the body must be, or None for "body not read, empty is fine"."""
    h = _headers(creds, headers)
    try:
        async with asyncio.timeout(TIMEOUT_S), _client() as c:
            r = await c.request(
                method,
                API + path,
                params={**(params or {}), "teamId": creds["org_id"]},
                json=json,
                content=content,
                headers=h,
            )
    except (httpx.HTTPError, TimeoutError) as e:
        raise VercelError("network", f"Vercel request failed ({type(e).__name__}).") from None
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 300:
        _raise(creds, r.status_code, data)
    if expect is not None and not isinstance(data, expect):
        raise VercelError("bad_response", "Vercel returned an unexpected response.", r.status_code)
    return data


def _pid(creds: dict) -> str:
    return creds.get("project_id", "")


def _rank(c: dict) -> float:
    try:
        return float(c.get("rank"))
    except (TypeError, ValueError):
        return float("inf")


def _q(v: str) -> str:
    return quote(v, safe="")


async def upsert_env(creds: dict, env: dict[str, str], targets: list[str]) -> None:
    items = [{"key": k, "value": v, "type": "encrypted", "target": targets} for k, v in env.items()]
    d = await _call(
        creds, "POST", f"/v10/projects/{_pid(creds)}/env", params={"upsert": "true"}, json=items, expect=(dict, list)
    )
    failed = d.get("failed") if isinstance(d, dict) else None
    if failed:  # bulk POST can be 2xx with per-item failures; never echo values
        err = (failed[0] or {}).get("error") or {}
        raise VercelError(str(err.get("code") or "env_failed"), f"Vercel rejected {len(failed)} env var(s).")


async def upload_file(creds: dict, data: bytes) -> str:
    sha = hashlib.sha1(data).hexdigest()  # noqa: S324 - Vercel's file digest is sha1
    await _call(
        creds,
        "POST",
        "/v2/files",
        content=data,
        headers={"x-vercel-digest": sha, "Content-Type": "application/octet-stream"},
        expect=None,
    )
    return sha


async def create_deployment(
    creds: dict, *, name: str, files: list[dict], target: str | None, build_command: str
) -> dict:
    body = {
        "name": name,
        "project": _pid(creds),
        "files": files,
        "projectSettings": {"framework": "nextjs", "buildCommand": build_command},
    }
    if target is not None:
        body["target"] = target
    d = await _call(
        creds, "POST", "/v13/deployments", params={"forceNew": "1", "skipAutoDetectionConfirmation": "1"}, json=body
    )
    return {k: d.get(k) for k in ("id", "url", "readyState")}


async def deployment_state(creds: dict, deployment_id: str) -> dict:
    d = await _call(creds, "GET", f"/v13/deployments/{_q(deployment_id)}")
    return {"readyState": d.get("readyState"), "url": d.get("url"), "errorMessage": d.get("errorMessage")}


async def deployment_events(creds: dict, deployment_id: str, *, limit: int = 50) -> list[dict]:
    """Build log lines, oldest -> newest (fetched newest-first so the cap keeps the tail)."""
    evs = await _call(
        creds,
        "GET",
        f"/v3/deployments/{_q(deployment_id)}/events",
        params={"limit": limit, "direction": "backward"},
        expect=list,
    )
    out = [
        {"at": e.get("created"), "message": e.get("text") or (e.get("payload") or {}).get("text") or ""}
        for e in evs
        if isinstance(e, dict)
    ]
    return [e for e in reversed(out) if e["message"]]


async def runtime_logs(creds: dict, deployment_id: str, *, limit: int = 50) -> list[dict]:
    """Last ``limit`` runtime log lines. Streamed; if the stream is still open at TIMEOUT_S, returns what was read."""
    h = _headers(creds)
    path = f"/v1/projects/{_pid(creds)}/deployments/{_q(deployment_id)}/runtime-logs"
    rows: list[dict] = []
    try:
        async with asyncio.timeout(TIMEOUT_S), _client() as c:
            async with c.stream("GET", API + path, params={"teamId": creds["org_id"]}, headers=h) as r:
                if r.status_code >= 300:
                    try:
                        data = json_loads((await r.aread()).decode() or "null")
                    except ValueError:
                        data = None
                    _raise(creds, r.status_code, data)
                async for ln in r.aiter_lines():
                    try:
                        row = json_loads(ln) if ln.strip() else None
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("message"):
                        rows.append({"at": row.get("timestampInMs"), "message": row["message"]})
                        del rows[:-limit]
    except TimeoutError:
        pass  # still-open stream: keep the tail collected so far
    except httpx.HTTPError as e:
        raise VercelError("network", f"Vercel request failed ({type(e).__name__}).") from None
    return rows


async def add_domain(creds: dict, name: str) -> dict:
    pid, n = _pid(creds), _q(name)
    try:
        d = await _call(creds, "POST", f"/v10/projects/{pid}/domains", json={"name": name})
    except VercelError as e:
        if e.status not in (400, 409):
            raise
        try:  # idempotent: already on THIS project is success; another project/team still raises
            d = await _call(creds, "GET", f"/v9/projects/{pid}/domains/{n}")
        except VercelError:
            raise e from None
    cfg = await _call(creds, "GET", f"/v6/domains/{n}/config", params={"projectIdOrName": pid})
    ranked = [c for c in cfg.get("recommendedCNAME") or [] if isinstance(c, dict) and c.get("value")]
    cname = min(ranked, key=_rank)["value"] if ranked else "cname.vercel-dns.com."
    return {"verified": d.get("verified"), "verification": d.get("verification", []), "cname": cname}


async def verify_domain(creds: dict, name: str) -> bool:
    d = await _call(creds, "POST", f"/v9/projects/{_pid(creds)}/domains/{_q(name)}/verify")
    return bool(d.get("verified"))


async def assign_alias(creds: dict, deployment_id: str, alias: str) -> None:
    await _call(creds, "POST", f"/v2/deployments/{_q(deployment_id)}/aliases", json={"alias": alias}, expect=None)


async def promote(creds: dict, deployment_id: str) -> None:
    """Point production at a deployment (no rebuild). Needed after a rollback: Vercel then stops
    auto-assigning production domains to new production deployments."""
    await _call(creds, "POST", f"/v10/projects/{_pid(creds)}/promote/{_q(deployment_id)}", expect=None)


async def rollback(creds: dict, deployment_id: str) -> None:
    await _call(creds, "POST", f"/v1/projects/{_pid(creds)}/rollback/{_q(deployment_id)}", expect=None)


async def disable_protection(creds: dict) -> None:
    await _call(
        creds,
        "PATCH",
        f"/v9/projects/{_pid(creds)}",
        json={"ssoProtection": None, "passwordProtection": None},
        expect=None,
    )


async def delete_project(creds: dict) -> None:
    await _call(creds, "DELETE", f"/v9/projects/{_pid(creds)}", expect=None)
