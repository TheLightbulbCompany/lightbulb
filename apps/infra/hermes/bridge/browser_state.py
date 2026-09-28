"""Small, authenticated-bridge-only adapter for agent-browser state handoff."""

from __future__ import annotations

import json
import os
import secrets
import re
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

MAX_STATE_BYTES = 50 * 1024 * 1024
COMMAND_TIMEOUT_S = 45
OPERATION_TIMEOUT_S = 100
CLEANUP_TIMEOUT_S = 5
#: A saved origin that does not load in this long is left out of the restore (its saved data
#: stays in the save). Before this, one unreachable site failed every restore, so the browser
#: never started again (dev 2026-09-28: a sleeping Heroku app, Page.navigate timed out).
ORIGIN_TIMEOUT_S = 10
#: What the state load and reload after the origins need, at least.
LOAD_RESERVE_S = 30
SESSION_NAME = "lb"
_CDP_PATH = re.compile(r"^/browser-streams/aws\.browser\.v1/sessions/[A-Za-z0-9]+/automation$")


def _valid_url(value: object, scheme: str) -> str:
    if not isinstance(value, str):
        raise ValueError("browser state URL is required")
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme != scheme or not host or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("browser state URL must use an AWS endpoint")
    if parsed.port not in (None, 443):
        raise ValueError("browser state URL must use HTTPS port 443")
    if scheme == "wss":
        if host != parsed.hostname or not re.fullmatch(r"bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com", host) or not _CDP_PATH.fullmatch(parsed.path):
            raise ValueError("invalid AgentCore CDP URL")
    elif not (host.endswith(".amazonaws.com") and (host.startswith("s3.") or ".s3." in host or ".s3-" in host)):
        raise ValueError("state URL must be an S3 endpoint")
    return value


@contextmanager
def _operation_env(cdp_url: str):
    # AgentCore runs UID 1000 with HOME=/; the CLI cannot create /.agent-browser.
    # A private operation directory also keeps daemon files out of saved profiles.
    with tempfile.TemporaryDirectory(prefix="lb-") as socket_dir:
        yield {
            key: value
            for key in ("PATH", "HOME", "TMPDIR")
            if isinstance((value := os.environ.get(key)), str) and value
        } | {
            "AGENT_BROWSER_CDP": cdp_url,
            "AGENT_BROWSER_SOCKET_DIR": socket_dir,
            "AGENT_BROWSER_NAMESPACE": f"lb-{secrets.token_hex(3)}",
            # One slow site must not use up the whole operation: see ORIGIN_TIMEOUT_S.
            "AGENT_BROWSER_DEFAULT_TIMEOUT": str(ORIGIN_TIMEOUT_S * 1000),
        }


def _remaining(deadline: float, cap: float = COMMAND_TIMEOUT_S) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("browser state operation timed out")
    return min(cap, remaining)


def _run(env: dict[str, str], *args: str, deadline: float) -> None:
    result = subprocess.run(
        ["agent-browser", "--session", SESSION_NAME, *args],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_remaining(deadline),
        check=False,
    )
    if result.returncode:
        raise RuntimeError("browser state command failed")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("browser state redirect refused")


_OPEN = urllib.request.build_opener(_NoRedirect)


def _download(url: str, deadline: float) -> bytes:
    with _OPEN.open(urllib.request.Request(_valid_url(url, "https")), timeout=_remaining(deadline)) as response:
        body = response.read(MAX_STATE_BYTES + 1)
    if len(body) > MAX_STATE_BYTES:
        raise ValueError("browser state is too large")
    return body


def _upload(url: str, body: bytes, deadline: float) -> None:
    request = urllib.request.Request(_valid_url(url, "https"), data=body, method="PUT")
    request.add_header("Content-Type", "application/json")
    with _OPEN.open(request, timeout=_remaining(deadline)):
        return


def _merge_state(current: dict, previous: dict) -> dict:
    current_origins = current.get("origins") if isinstance(current.get("origins"), list) else []
    previous_origins = previous.get("origins") if isinstance(previous.get("origins"), list) else []
    merged = {o.get("origin"): o for o in previous_origins if isinstance(o, dict) and isinstance(o.get("origin"), str)}
    merged.update({o.get("origin"): o for o in current_origins if isinstance(o, dict) and isinstance(o.get("origin"), str)})
    return {**current, "origins": list(merged.values())}


def _valid_state(body: bytes) -> dict:
    if len(body) > MAX_STATE_BYTES:
        raise ValueError("browser state is too large")
    parsed = json.loads(body)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("cookies"), list) or not isinstance(parsed.get("origins"), list):
        raise ValueError("browser state is invalid")
    return parsed


def _state_origins(state: dict) -> list[str]:
    origins: list[str] = []
    for item in state["origins"]:
        origin = item.get("origin") if isinstance(item, dict) else None
        if not isinstance(origin, str):
            raise ValueError("browser state origin is invalid")
        parsed = urlparse(origin)
        try:
            parsed.port
        except ValueError:
            raise ValueError("browser state origin is invalid") from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or origin != f"{parsed.scheme}://{parsed.netloc}"):
            raise ValueError("browser state origin is invalid")
        if origin not in origins:
            origins.append(origin)
    return origins


def checkpoint(params: dict) -> dict:
    """Save or load state for the already-connected named browser daemon session."""
    if not isinstance(params, dict):
        raise ValueError("browser state params are required")
    action = params.get("action")
    if action not in ("save", "load"):
        raise ValueError("browser state action must be save or load")
    cdp_url = _valid_url(params.get("cdpUrl"), "wss")
    state_url = _valid_url(params.get("stateUrl"), "https")
    previous_url = params.get("previousStateUrl")
    path: str | None = None
    with _operation_env(cdp_url) as env:
        deadline = time.monotonic() + OPERATION_TIMEOUT_S
        load_body: bytes | None = None
        origins: list[str] = []
        try:
            if action == "load":
                load_body = _download(state_url, deadline)
                origins = _state_origins(_valid_state(load_body))
        except ValueError:
            raise
        except Exception:
            raise RuntimeError("browser state checkpoint failed") from None
        try:
            with tempfile.NamedTemporaryFile(prefix="lightbulb-browser-", suffix=".json", mode="w+b", delete=False) as tmp:
                path = tmp.name
            os.chmod(path, 0o600)
            if action == "save":
                _run(env, "connect", cdp_url, deadline=deadline)
                _run(env, "state", "save", path, deadline=deadline)
                body = Path(path).read_bytes()
                current = _valid_state(body)
                # ponytail: this fresh client exports its visible origins; merging retains prior
                # checkpoints. A persistent observer is needed for unsaved, closed-origin changes.
                if previous_url:
                    previous = _valid_state(_download(_valid_url(previous_url, "https"), deadline))
                    current = _merge_state(current, previous)
                    body = json.dumps(current, separators=(",", ":")).encode()
                    if len(body) > MAX_STATE_BYTES:
                        raise ValueError("browser state is too large")
                _upload(state_url, body, deadline)
                return {"ok": True, "bytes": len(body)}
            if action == "load":
                _run(env, "connect", cdp_url, deadline=deadline)
                # agent-browser 0.38.1's state load only adds cookies when the file has any;
                # an empty saved jar must still replace, rather than inherit, profile cookies.
                _run(env, "cookies", "clear", deadline=deadline)
                # State load only sets listed keys. Clear every saved origin first so an empty
                # localStorage snapshot remains an authoritative logout rather than a no-op.
                reached = set()
                for origin in origins:
                    if deadline - time.monotonic() < ORIGIN_TIMEOUT_S + LOAD_RESERVE_S:
                        break  # the cookies (the sign-ins) matter most: keep time to load them
                    try:
                        _run(env, "open", origin, deadline=min(deadline, time.monotonic() + ORIGIN_TIMEOUT_S + 2))
                        _run(env, "storage", "local", "clear", deadline=deadline)
                        reached.add(origin)
                    except TimeoutError:
                        raise
                    except Exception:  # noqa: BLE001 -- that site is down or slow: restore the rest
                        pass
                state = _valid_state(load_body or b"")
                # State load navigates to each origin it sets localStorage for: only reachable ones.
                state["origins"] = [o for o in state["origins"] if o.get("origin") in reached]
                Path(path).write_bytes(json.dumps(state, separators=(",", ":")).encode())
                _run(env, "state", "load", path, deadline=deadline)
                _run(env, "reload", deadline=deadline)
                return {"ok": True, "bytes": 0}
        except ValueError:
            raise
        except Exception as exc:
            raise RuntimeError("browser state checkpoint failed") from None
        finally:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                finally:
                    try:
                        _run(env, "close", deadline=time.monotonic() + CLEANUP_TIMEOUT_S)
                    except Exception:
                        pass
