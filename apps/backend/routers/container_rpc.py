"""
Generic RPC proxy to user's OpenClaw gateway container.

Single endpoint: POST /rpc accepts { method, params } and forwards
to the user's container via short-lived WebSocket connection.
Gateway tokens stay server-side — never exposed to the browser.

The OpenClaw gateway requires a connect handshake before accepting
RPC calls: receive connect.challenge → send connect with auth token
→ receive hello-ok → then send RPC request.
"""

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field
from websockets import connect as ws_connect

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.containers import get_ecs_manager, get_workspace
from core.containers.ecs_manager import GATEWAY_PORT

logger = logging.getLogger(__name__)

router = APIRouter()

_WS_TIMEOUT = 30  # seconds


class RpcRequest(BaseModel):
    method: str = Field(..., description="RPC method name (e.g. 'health', 'agents.list')")
    params: Optional[Dict[str, Any]] = Field(default=None, description="Optional method parameters")


async def _openclaw_handshake(ws, token: str) -> None:
    """Complete the OpenClaw gateway connect handshake.

    1. Receive connect.challenge from gateway
    2. Send connect request with auth token
    3. Verify hello-ok response

    Raises:
        RuntimeError: If handshake fails.
    """
    # Step 1: receive connect.challenge
    raw = await asyncio.wait_for(ws.recv(), timeout=10)
    challenge = json.loads(raw)
    if challenge.get("event") != "connect.challenge":
        raise RuntimeError(f"Expected connect.challenge, got: {challenge.get('event', 'unknown')}")

    # Step 2: send connect
    connect_msg = {
        "type": "req",
        "id": str(uuid.uuid4()),
        "method": "connect",
        "params": {
            "minProtocol": 4,
            "maxProtocol": 4,
            "client": {
                "id": "cli",
                "version": "1.0.0",
                "platform": "linux",
                "mode": "cli",
            },
            "role": "operator",
            "scopes": ["operator.admin"],
            "auth": {"token": token},
        },
    }
    await ws.send(json.dumps(connect_msg))

    # Step 3: verify hello-ok
    resp_raw = await asyncio.wait_for(ws.recv(), timeout=10)
    resp = json.loads(resp_raw)
    if not resp.get("ok"):
        err = resp.get("error", {}).get("message", "unknown error")
        raise RuntimeError(f"Gateway connect failed: {err}")


async def _call_gateway_rpc(
    ip: str,
    token: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """Open a short-lived WebSocket to the gateway, send RPC call, return response."""
    uri = f"ws://{ip}:{GATEWAY_PORT}"

    async with ws_connect(uri, open_timeout=_WS_TIMEOUT, close_timeout=5) as ws:
        # Complete OpenClaw connect handshake first
        await _openclaw_handshake(ws, token)

        # Now send the actual RPC request
        req_id = str(uuid.uuid4())
        rpc_msg = {
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        await ws.send(json.dumps(rpc_msg))

        # Read messages until we get the matching RPC response.
        # The gateway may send event broadcasts (health, state) before
        # responding to our request — skip those.
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT)
            data = json.loads(raw)
            if data.get("type") == "res" and data.get("id") == req_id:
                if not data.get("ok"):
                    err_msg = data.get("error", {}).get("message", "RPC call rejected")
                    raise RuntimeError(f"Gateway RPC error: {err_msg}")
                return data.get("payload", {})


@router.post(
    "/gateway/restart",
    summary="Restart the OpenClaw gateway on the user's container",
    description=(
        "Sends a config.apply RPC to the user's running gateway container, "
        "which triggers a gateway process restart. "
        "Use this when the WebSocket connection is down but the container is running."
    ),
    operation_id="gateway_restart",
    responses={
        404: {"description": "No running container for this user"},
        502: {"description": "Gateway is not responding"},
    },
)
async def gateway_restart(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    ecs_manager = get_ecs_manager()
    container, ip = await ecs_manager.resolve_running_container(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="No running container")
    if not ip:
        raise HTTPException(status_code=502, detail="Container gateway is starting up")

    try:
        await _call_gateway_rpc(
            ip=ip,
            token=container["gateway_token"],
            method="config.apply",
            params={},
        )
    except ConnectionRefusedError:
        raise HTTPException(status_code=502, detail="Gateway is not responding")
    except TimeoutError:
        raise HTTPException(status_code=502, detail="Gateway restart timed out")
    except Exception as e:
        logger.error("Gateway restart failed for user %s: %s", owner_id, e)
        raise HTTPException(status_code=502, detail="Gateway restart failed")

    return {"ok": True}


@router.post(
    "/rpc",
    summary="[Deprecated] Proxy RPC call to user's OpenClaw container",
    description=(
        "DEPRECATED: Use the WebSocket connection at /api/v1/ws with "
        "{type: 'req', id, method, params} messages instead. "
        "This HTTP fallback will be removed in a future release."
    ),
    deprecated=True,
    operation_id="container_rpc",
    responses={
        404: {"description": "No running container for this user"},
        502: {"description": "Gateway connection or RPC call failed"},
    },
)
async def container_rpc(
    body: RpcRequest,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)

    # Look up container and discover task IP
    ecs_manager = get_ecs_manager()
    container, ip = await ecs_manager.resolve_running_container(owner_id)
    if not container:
        raise HTTPException(
            status_code=404,
            detail="No running container. Subscribe to access the control panel.",
        )
    if not ip:
        raise HTTPException(status_code=502, detail="Container gateway is starting up")

    try:
        result = await _call_gateway_rpc(
            ip=ip,
            token=container["gateway_token"],
            method=body.method,
            params=body.params,
        )
    except ConnectionRefusedError:
        logger.error("Gateway refused connection for user %s at %s", owner_id, ip)
        raise HTTPException(status_code=502, detail="Container gateway is not responding")
    except TimeoutError:
        logger.error("Gateway timeout for user %s at %s", owner_id, ip)
        raise HTTPException(status_code=502, detail="Container gateway timed out")
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON from gateway for user %s: %s", owner_id, e)
        raise HTTPException(status_code=502, detail="Invalid response from container gateway")
    except Exception as e:
        logger.error("RPC call failed for user %s: %s", owner_id, e)
        raise HTTPException(status_code=502, detail="Gateway RPC call failed")

    return {"result": result}


# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file
MAX_FILES_PER_REQUEST = 10
SAFE_FILENAME_RE = re.compile(r"^[\w\-. ]+$")


def _sanitize_filename(name: str) -> str:
    """Return a safe filename, stripping path components and invalid chars."""
    # Take only the basename (no directory traversal)
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not base or not SAFE_FILENAME_RE.match(base):
        # Replace unsafe chars
        base = re.sub(r"[^\w\-. ]", "_", base) or "upload"
    return base


@router.post(
    "/files",
    summary="Upload files to the user's agent workspace",
    description=(
        "Uploads one or more files to the user's workspace on EFS. "
        "Files are placed in the agent's `workspaces/{agent_id}/uploads/` directory "
        "and are accessible to the user's OpenClaw agent. "
        "Max 10MB per file, 10 files per request."
    ),
    operation_id="upload_files",
    responses={
        400: {"description": "File too large or too many files"},
        404: {"description": "No container for this user"},
    },
)
async def upload_files(
    files: List[UploadFile] = File(..., description="Files to upload"),
    agent_id: str = Query(..., description="Target agent ID"),
    auth: AuthContext = Depends(get_current_user),
):
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {MAX_FILES_PER_REQUEST} per request.",
        )

    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise HTTPException(status_code=400, detail="Invalid agent_id")

    owner_id = resolve_owner_id(auth)

    # Verify user has a container
    ecs_manager = get_ecs_manager()
    container = await ecs_manager.get_service_status(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="No container found")

    workspace = get_workspace()
    workspace.ensure_user_dir(owner_id)

    uploaded = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit.",
            )

        safe_name = _sanitize_filename(f.filename or "upload")
        dest_path = f"workspaces/{agent_id}/uploads/{safe_name}"
        workspace.write_bytes(owner_id, dest_path, data)
        # The agent's working dir is $HOME (/home/node) but EFS is mounted
        # at $HOME/.openclaw, so the agent sees uploads relative to that mount.
        agent_path = f".openclaw/{dest_path}"
        uploaded.append({"filename": safe_name, "path": agent_path, "size": len(data)})
        logger.info("Uploaded %s (%d bytes) for user %s", dest_path, len(data), owner_id)

    return {"uploaded": uploaded}
