"""Small, authenticated-bridge-only adapter for agent-browser state handoff."""

from __future__ import annotations

import hashlib
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
ORIGIN_TIMEOUT_S = 5
#: One origin's whole step: open, clear, and load its localStorage (two navigations).
ORIGIN_STEP_S = 8
#: Every origin together. The sign-ins (cookies) load BEFORE this phase, so running out of it
#: only costs some sites' localStorage. Without its own cap, 8 heavy origins (prod 2026-09-28)
#: ate the operation and the restore failed after 105 s, sign-ins and all.
ORIGINS_BUDGET_S = 20
#: What the reload, post-load export and base upload after the origins need, at least.
LOAD_RESERVE_S = 30
#: Below this, the cosmetic reload is skipped so the export and upload still fit.
RELOAD_MIN_S = 20
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


def _open_origins(env: dict[str, str], deadline: float) -> set[str]:
    """Origins of the session's open tabs, so an origin missing from the export can be told
    apart: open means its storage was cleared, closed means it is merely not visible. Any
    failure answers "none open", which never deletes."""
    try:
        result = subprocess.run(
            ["agent-browser", "--session", SESSION_NAME, "--json", "tab", "list"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_remaining(deadline, 10),
            check=False,
        )
        if result.returncode:
            return set()
        found: set[str] = set()

        def walk(value) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "url" and isinstance(item, str):
                        parsed = urlparse(item)
                        if parsed.scheme in ("http", "https") and parsed.netloc:
                            found.add(f"{parsed.scheme}://{parsed.netloc}")
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(json.loads(result.stdout or b"null"))
        return found
    except Exception:  # noqa: BLE001
        return set()


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


_EMPTY_STATE = {"cookies": [], "origins": []}


def _digest(items: list) -> str:
    return hashlib.sha256(json.dumps(sorted(items), separators=(",", ":")).encode()).hexdigest()


def _cookies_digest(cookies: list) -> str:
    # The whole persisted cookie, every key (partitionKey included): the base is this browser's
    # own export, so Chrome's normalisation is already in it and only real changes differ.
    return _digest([json.dumps(c, sort_keys=True, separators=(",", ":")) for c in cookies if isinstance(c, dict)])


def _storage_digest(origin: dict | None) -> str:
    items = (origin or {}).get("localStorage")
    return _digest([json.dumps([i.get("name"), i.get("value")]) for i in items or [] if isinstance(i, dict)])


def _by_domain(state: dict) -> dict:
    out: dict = {}
    for c in state.get("cookies") or []:
        if isinstance(c, dict) and isinstance(c.get("domain"), str):
            out.setdefault(c["domain"], []).append(c)
    return out


def _by_origin(state: dict) -> dict:
    return {o["origin"]: o for o in state.get("origins") or [] if isinstance(o, dict) and isinstance(o.get("origin"), str)}


def _merge_three(base: dict, current: dict, previous: dict, open_origins=frozenset()) -> dict:
    """Per cookie domain and localStorage origin, this session's version wins only where it
    changed since ``base`` (what the session last restored or saved); everything else keeps
    ``previous``, the workspace save -- which a paired Mac may have written in the meantime."""
    b_c, c_c, p_c = _by_domain(base), _by_domain(current), _by_domain(previous)
    cookies: list = []
    for domain in sorted(set(b_c) | set(c_c) | set(p_c)):
        ours = c_c.get(domain, [])
        cookies += ours if _cookies_digest(ours) != _cookies_digest(b_c.get(domain, [])) else p_c.get(domain, [])
    b_o, origins, c_o = _by_origin(base), _by_origin(previous), _by_origin(current)
    skipped = set(base.get("skippedOrigins") or ())
    # An export omits an origin whose last key was cleared. It counts as cleared only when this
    # session could see it (in its own export-derived base) and its page is open now; an
    # invisible, skipped or closed origin keeps carrying the workspace's copy.
    cleared = set(base.get("visibleOrigins") or ()) & set(open_origins)
    for name in set(c_o) | (cleared - set(c_o)):
        if name in skipped:
            continue
        origin = c_o.get(name) or {"origin": name, "localStorage": []}
        if _storage_digest(origin) != _storage_digest(b_o.get(name)):
            origins[name] = origin
    return {**current, "cookies": cookies, "origins": list(origins.values())}


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
    if action not in ("save", "load", "base"):
        raise ValueError("browser state action must be save, load or base")
    cdp_url = _valid_url(params.get("cdpUrl"), "wss")
    state_url = _valid_url(params.get("stateUrl"), "https")
    previous_url = params.get("previousStateUrl")
    for extra in ("baseOutUrl", "baseStateUrl"):
        if params.get(extra) is not None:
            _valid_url(params[extra], "https")
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
            if action == "base":
                # A session started with nothing to restore still needs a merge base: its own
                # export now, so its first save writes only what it changes.
                _run(env, "connect", cdp_url, deadline=deadline)
                _run(env, "state", "save", path, deadline=deadline)
                exported = _valid_state(Path(path).read_bytes())
                exported["visibleOrigins"] = sorted(_by_origin(exported))
                _upload(state_url, json.dumps(exported, separators=(",", ":")).encode(), deadline)
                return {"ok": True, "bytes": 0, "base": True}
            if action == "save":
                _run(env, "connect", cdp_url, deadline=deadline)
                open_origins = _open_origins(env, deadline) if params.get("baseOutUrl") else set()
                _run(env, "state", "save", path, deadline=deadline)
                body = Path(path).read_bytes()
                current = _valid_state(body)
                base_out = params.get("baseOutUrl")
                if base_out:
                    # Three-way: the backend keeps this raw export as the next save's base.
                    # No base yet (a session restored before this image) counts as empty, which
                    # never deletes what others saved. An UNREADABLE base fails the checkpoint
                    # (the backend retries): guessing would let this session overwrite them.
                    base = _EMPTY_STATE
                    if params.get("baseStateUrl"):
                        base = _valid_state(_download(_valid_url(params["baseStateUrl"], "https"), deadline))
                    previous = _valid_state(_download(_valid_url(previous_url, "https"), deadline)) if previous_url else _EMPTY_STATE
                    merged = json.dumps(
                        _merge_three(base, current, previous, open_origins), separators=(",", ":")
                    ).encode()
                    if len(merged) > MAX_STATE_BYTES:
                        raise ValueError("browser state is too large")
                    # Both keys are this checkpoint's own; the backend publishes them together
                    # only on commit, and deletes both if anything after this fails.
                    # The next base: this export plus the origins this client cannot see, carried
                    # from the old base, so an unseen origin's stale copy never looks like a change.
                    seen = set(_by_origin(current))
                    skipped = set(base.get("skippedOrigins") or ())
                    # Origins this save published as cleared carry an explicit empty value, so
                    # re-creating their old values later is a change, not a match with the base.
                    cleared = (set(base.get("visibleOrigins") or ()) & open_origins) - seen - skipped
                    next_base = {
                        **current,
                        "origins": list(_by_origin(current).values())
                        + [
                            {"origin": name, "localStorage": []} if name in cleared else o
                            for name, o in _by_origin(base).items()
                            if name not in seen
                        ]
                        + [{"origin": n, "localStorage": []} for n in sorted(cleared - set(_by_origin(base)))],
                        "skippedOrigins": [n for n in skipped if n not in seen],
                        "visibleOrigins": sorted(seen | cleared),
                    }
                    _upload(state_url, merged, deadline)
                    _upload(base_out, json.dumps(next_base, separators=(",", ":")).encode(), deadline)
                    return {"ok": True, "bytes": len(merged), "base": True}
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
                state = _valid_state(load_body or b"")
                # The cookies (the sign-ins) first, on their own: no navigation, so no slow site
                # can cost them.
                Path(path).write_bytes(json.dumps({**state, "origins": []}, separators=(",", ":")).encode())
                _run(env, "state", "load", path, deadline=deadline)
                # Then each saved origin, within ORIGINS_BUDGET_S: cleared first so an empty
                # snapshot stays an authoritative logout, then its own localStorage loaded.
                reached = set()
                saved = _by_origin(state)
                origins_deadline = min(time.monotonic() + ORIGINS_BUDGET_S, deadline - LOAD_RESERVE_S)
                for origin in origins:
                    if origins_deadline - time.monotonic() < 1:
                        break
                    step = min(origins_deadline, time.monotonic() + ORIGIN_STEP_S)
                    try:
                        _run(env, "open", origin, deadline=step)
                        _run(env, "storage", "local", "clear", deadline=step)
                        Path(path).write_bytes(json.dumps({"cookies": [], "origins": [saved[origin]]},
                                                          separators=(",", ":")).encode())
                        _run(env, "state", "load", path, deadline=step)
                        reached.add(origin)
                    except Exception:  # noqa: BLE001 -- that site is down or slow: restore the rest
                        pass
                state["origins"] = [o for o in state["origins"] if o.get("origin") in reached]
                if deadline - time.monotonic() >= RELOAD_MIN_S:
                    try:  # cosmetic: the open tab picks up the sign-ins; never worth the restore
                        _run(env, "reload", deadline=min(deadline, time.monotonic() + ORIGIN_STEP_S))
                    except Exception:  # noqa: BLE001
                        pass
                base_out = params.get("baseOutUrl")
                if not base_out:
                    return {"ok": True, "bytes": 0}
                # The merge base is the browser's OWN export right after the load, so Chrome's
                # normalisation (the 400-day expiry cap, sameSite defaults) is part of it and
                # the next save flags only real changes. localStorage is not normalised: origins
                # this client cannot see keep the loaded copy.
                _run(env, "state", "save", path, deadline=deadline)
                exported = _valid_state(Path(path).read_bytes())
                seen = set(_by_origin(exported))
                # Origins the restore skipped (a slow site) hold unknown data in this session:
                # the next save never writes them.
                base = {**exported, "origins": list(_by_origin(exported).values())
                        + [o for name, o in _by_origin(state).items() if name not in seen],
                        "skippedOrigins": sorted(set(origins) - reached),
                        "visibleOrigins": sorted(seen)}
                _upload(base_out, json.dumps(base, separators=(",", ":")).encode(), deadline)
                return {"ok": True, "bytes": 0, "base": True}
        except ValueError:
            raise
        except Exception:
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
