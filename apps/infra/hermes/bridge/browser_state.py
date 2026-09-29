"""Small, authenticated-bridge-only adapter for agent-browser state handoff.

One engine for both browsers: the cloud session (its AgentCore automation URL) and a paired Mac's
agent Chrome (``source: "mac"``, reached through the run's own ``cdp_mux`` pipe). A Mac load carries
cookies and localStorage like the cloud's, but loads each origin's localStorage in a background tab
whose every request is answered here (``_mac_origins``): no network request, no site script,
nothing in front of the person. Every Mac export is vetted before it can reach the workspace save.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
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
#: What a save's merge downloads and uploads need after its closed-origin reads, at least.
SAVE_RESERVE_S = 15
#: Below this, the cosmetic reload is skipped so the export and upload still fit.
RELOAD_MIN_S = 20
SESSION_NAME = "lb"
#: A cdp_mux loopback endpoint (cdp_mux.PORT, PREFIX, key_for): how the bridge reaches a Mac's pipe.
_MUX_URL = re.compile(r"^ws://127\.0\.0\.1:9222/devtools/browser/[0-9a-f]{24}$")
#: Mac exports (spec §3, as desktop_browser_state._vetted): what a cloud browser can always load.
MAX_COOKIES = 5000
MAX_COOKIE_BYTES = 4096
MAX_ITEM_BYTES = 1024 * 1024
MAX_ORIGIN_BYTES = 10 * 1024 * 1024
SAME_SITE = {"Strict", "Lax", "None"}
COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey")
#: More loaded domains than this vanishing at once from a Mac is a reset profile, not sign-outs.
MASS_SIGNOUT_MIN = 5
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


def _valid_cdp_url(value: object, source: str = "cloud") -> str:
    """The cloud's AgentCore automation URL, or for ``source`` "mac" only a cdp_mux loopback
    endpoint (the run's Mac pipe). The Mac's door URL itself never reaches here."""
    if source == "mac":
        if isinstance(value, str) and _MUX_URL.fullmatch(value):
            return value
        raise ValueError("a Mac checkpoint needs the bridge's loopback CDP endpoint")
    return _valid_url(value, "wss")


def _good_cookie(c) -> bool:
    return (
        isinstance(c, dict)
        and all(isinstance(c.get(k), str) for k in ("name", "value", "domain", "path"))
        and ("partitionKey" not in c or isinstance(c["partitionKey"], str))
        and len((c["name"] + c["value"] + c["domain"] + c["path"] + c.get("partitionKey", "")).encode())
        <= MAX_COOKIE_BYTES
        and type(c.get("expires")) in (int, float)
        and math.isfinite(c["expires"])
        and type(c.get("httpOnly")) is bool
        and type(c.get("secure")) is bool
        and ("sameSite" not in c or c["sameSite"] in SAME_SITE)
    )


def _good_storage(items) -> bool:
    if not isinstance(items, list):
        return False
    total = 0
    for i in items:
        if not (isinstance(i, dict) and isinstance(i.get("name"), str) and isinstance(i.get("value"), str)):
            return False
        size = len(i["name"].encode()) + len(i["value"].encode())
        total += size
        if size > MAX_ITEM_BYTES or total > MAX_ORIGIN_BYTES:
            return False
    return True


def _good_origin(name) -> bool:
    try:
        _state_origins({"origins": [{"origin": name}]})
        return True
    except ValueError:
        return False


def _vet(state: dict) -> tuple[dict, set[str]]:
    """A Mac export -> (what a browser can always load, what to keep from the workspace). A bad
    cookie drops out and its whole domain keeps the workspace's copy (never a sign-out); a bad
    origin drops out and keeps the workspace's copy (unread). Counts are never logged with values."""
    cookies, keep = [], set()
    for n, c in enumerate(state.get("cookies") or []):
        if isinstance(c, dict):
            c = {k: v for k, v in c.items() if v is not None}  # null means absent
        if n < MAX_COOKIES and _good_cookie(c):
            cookies.append({k: c[k] for k in COOKIE_KEYS if k in c})
        elif isinstance(c, dict) and isinstance(c.get("domain"), str):
            keep.add(c["domain"])
    origins = {}
    for o in state.get("origins") or []:
        name = o.get("origin") if isinstance(o, dict) else None
        if _good_origin(name) and _good_storage(o.get("localStorage")):
            origins[name] = {"origin": name, "localStorage": [{"name": i["name"], "value": i["value"]}
                                                              for i in o["localStorage"]]}
        elif isinstance(name, str):
            keep.add(name)  # an origin never collides with a cookie domain (it has a scheme)
    return {"cookies": cookies, "origins": list(origins.values())}, keep


#: Makes one origin's localStorage equal the saved items, as a diff: keys the save lacks are removed
#: (an empty save still ends empty -- an authoritative logout) and only changed values are set. Never
#: clear(): its `storage` event (key null) logs the person's open tabs of that site out. Touches
#: nothing unless the page is the expected origin (a late load event from an earlier navigation
#: must never write one origin's items into another); answers the origin it changed, else null.
_SET_STORAGE_JS = ("((expected, items) => { if (location.origin !== expected) return null;"
                   " const want = new Map(items);"
                   " for (const k of Object.keys(localStorage)) if (!want.has(k)) localStorage.removeItem(k);"
                   " for (const [k, v] of want) if (localStorage.getItem(k) !== v) localStorage.setItem(k, v);"
                   " return expected; })")
#: Every request of the background tab, answered locally: an empty page, never the site.
_EMPTY_PAGE = {"responseCode": 200, "responseHeaders": [{"name": "Content-Type", "value": "text/html"}], "body": ""}


#: Reads one origin's localStorage in its (empty, locally answered) page; null if the page is
#: not that origin.
_GET_STORAGE_JS = "((expected) => location.origin === expected ? Object.entries(localStorage) : null)"


def _mac_origins(cdp_url: str, saved: list[dict], deadline: float) -> set[str]:
    """Load each saved origin's localStorage on the Mac without showing or fetching anything
    (``_in_background_tab``). agent-browser's own ``state load`` sets localStorage on its current
    page only, so it would need a visible navigation per origin. Returns the origins loaded; the
    rest are left out (skipped), as a slow site is on the cloud."""
    jobs = []
    for origin in saved:
        items = [[i["name"], i["value"]] for i in origin["localStorage"]]
        jobs.append((origin["origin"], f"{_SET_STORAGE_JS}({json.dumps(origin['origin'])}, {json.dumps(items)})"))
    return {o for o, value in _in_background_tab(cdp_url, jobs, deadline).items() if value == o}


def _read_origins(cdp_url: str, origins: list[str], deadline: float) -> dict[str, list]:
    """Each origin's localStorage, read the same way (never fetching the site): origin -> its
    items as [{"name", "value"}]. An origin that fails or answers malformed is left out."""
    found = {}
    jobs = [(o, f"{_GET_STORAGE_JS}({json.dumps(o)})") for o in origins]
    for origin, pairs in _in_background_tab(cdp_url, jobs, deadline).items():
        if isinstance(pairs, list) and all(isinstance(p, list) and len(p) == 2 for p in pairs):
            items = [{"name": k, "value": v} for k, v in pairs]
            if _good_storage(items):
                found[origin] = items
    return found


def _closed_origins(env: dict[str, str], cdp_url: str, mac: bool, current: dict,
                    open_origins: set[str], deadline: float) -> dict[str, tuple]:
    """Add to ``current`` the localStorage of origins pages visited that no open page shows any
    more (a tab closed, or navigated away): the export reads open pages only. Answers what was
    read (origin -> (items, landing number)); one that fails stays visited for the next save. An empty read is a
    real clear (a logout, then the tab left): the read script answers only in the expected
    origin's page, so a failed or wrong-origin read is absent, never empty."""
    from bridge import cdp_mux

    shown = set(_by_origin(current)) | open_origins
    visited = cdp_mux.visited_origins(cdp_url if mac else None)
    # Newest landing first: an origin whose read keeps timing out keeps its old number and falls
    # behind the ones a run just left. ponytail: 3+ new origins that all time out still fill
    # ORIGINS_BUDGET_S; a per-origin failure count if that shows up.
    closed = sorted((o for o in visited if o not in shown), key=visited.get, reverse=True)
    if not closed:
        return {}
    budget = min(deadline - SAVE_RESERVE_S, time.monotonic() + ORIGINS_BUDGET_S)
    if budget - time.monotonic() < 1:
        return {}
    _run(env, "close", deadline=time.monotonic() + CLEANUP_TIMEOUT_S)  # the cloud allows one CDP client
    try:
        found = _read_origins(cdp_url, closed, budget)
    except Exception:  # noqa: BLE001 -- the export alone still saves; these retry next time
        return {}
    current["origins"] = list(current.get("origins") or []) + [
        {"origin": o, "localStorage": items} for o, items in found.items()]
    # Each read carries the landing it saw: a page landing there again during this save is a
    # later landing, and forget_origins keeps it for the next save to read.
    # ponytail: a person's visit while no agent is connected (the mux sees no traffic then) is
    # never a landing at all; its page is still read if it is open at the save.
    return {o: (items, visited[o]) for o, items in found.items()}


def _in_background_tab(cdp_url: str, jobs: list[tuple[str, str]], deadline: float) -> dict:
    """Run each (origin, script) in that origin's page without showing or fetching anything: a
    background tab (``Target.createTarget`` background), ``Fetch`` pausing its every request and
    answering it with an empty page, then the script evaluated there; the tab is closed after.
    Answers origin -> the script's value, for the origins that answered in time."""
    from websockets.sync.client import connect

    reached: dict = {}
    if not jobs or deadline - time.monotonic() < 1:
        return reached
    ids = itertools.count(1)
    with connect(cdp_url, max_size=None, open_timeout=_remaining(deadline, 10)) as ws:
        page = {"session": None, "loaded": False}

        def receive(until: float) -> dict:
            """The next message, after answering the background tab's paused requests."""
            message = json.loads(ws.recv(timeout=_remaining(until)))
            if page["session"] and message.get("sessionId") == page["session"]:
                if message.get("method") == "Fetch.requestPaused":
                    ws.send(json.dumps({"id": next(ids), "method": "Fetch.fulfillRequest", "sessionId": page["session"],
                                        "params": {"requestId": message["params"]["requestId"], **_EMPTY_PAGE}}))
                elif message.get("method") == "Page.loadEventFired":
                    page["loaded"] = True
            return message

        def call(method: str, params: dict | None = None, session: str | None = None, until: float = deadline) -> dict:
            uid = next(ids)
            ws.send(json.dumps({"id": uid, "method": method, "params": params or {}, **({"sessionId": session} if session else {})}))
            while True:
                message = receive(until)
                if message.get("id") == uid:
                    if "error" in message:
                        raise RuntimeError("browser state CDP call failed")
                    return message.get("result") or {}

        target = None
        try:
            target = call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
            page["session"] = call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            # A service worker answers a navigation before Fetch sees it (and runs site script):
            # bypass them in this tab first.
            call("Network.setBypassServiceWorker", {"bypass": True}, page["session"])
            call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, page["session"])
            call("Page.enable", {}, page["session"])
            for origin, script in jobs:
                if deadline - time.monotonic() < 1:
                    break
                step = min(deadline, time.monotonic() + ORIGIN_STEP_S)
                try:
                    page["loaded"] = False
                    call("Page.navigate", {"url": origin + "/"}, page["session"], step)
                    while not page["loaded"]:  # the empty page's load: its document is the origin's now
                        receive(step)
                    answer = call("Runtime.evaluate", {"expression": script,
                                                       "returnByValue": True}, page["session"], step)
                    reached[origin] = (answer.get("result") or {}).get("value")
                except Exception:  # noqa: BLE001 -- that origin stays out (skipped)
                    pass
        finally:
            try:
                if target:
                    call("Target.closeTarget", {"targetId": target}, until=time.monotonic() + CLEANUP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 -- a stray background tab is harmless
                pass
    return reached


def _forget_committed(cdp_url: str | None, read: dict[str, tuple], previous: dict) -> None:
    """Forget a closed origin only once the workspace's COMMITTED save (``previous``) already
    holds what was just read: this save's own upload is not authoritative (the backend commits
    after this RPC returns, and deletes the objects if the commit loses), so an origin read now
    is read again next save and dropped once it shows up committed (a clear: committed empty or
    absent). No page shows these origins, so their storage cannot change meanwhile."""
    from bridge import cdp_mux

    committed = _by_origin(previous)
    done = {o: landing for o, (items, landing) in read.items()
            if ((committed.get(o) or {}).get("localStorage") or []) == items}
    cdp_mux.forget_origins(cdp_url, done)


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


def _merge_three(base: dict, current: dict, previous: dict, open_origins=frozenset(), *,
                 keep=frozenset(), hold_back: bool = False) -> dict:
    """Per cookie domain and localStorage origin, this session's version wins only where it
    changed since ``base`` (what the session last restored or saved); everything else keeps
    ``previous``, the workspace save -- which a paired Mac may have written in the meantime.
    ``keep`` (domains and origins) always takes ``previous``; ``hold_back`` treats more than
    max(MASS_SIGNOUT_MIN, half the base's domains) vanishing at once as a broken profile and keeps
    ``previous`` for them."""
    b_c, c_c, p_c = _by_domain(base), _by_domain(current), _by_domain(previous)
    keep = set(keep)
    if hold_back:
        lost = {d for d in b_c if d not in c_c and d not in keep}
        if len(lost) > max(MASS_SIGNOUT_MIN, len(b_c) // 2):
            keep |= lost
    cookies: list = []
    for domain in sorted(set(b_c) | set(c_c) | set(p_c)):
        ours = c_c.get(domain, [])
        changed = domain not in keep and _cookies_digest(ours) != _cookies_digest(b_c.get(domain, []))
        cookies += ours if changed else p_c.get(domain, [])
    b_o, origins, c_o = _by_origin(base), _by_origin(previous), _by_origin(current)
    skipped = set(base.get("skippedOrigins") or ())
    # An export omits an origin whose last key was cleared. It counts as cleared only when this
    # session could see it (in its own export-derived base) and its page is open now; an
    # invisible, skipped or closed origin keeps carrying the workspace's copy.
    cleared = set(base.get("visibleOrigins") or ()) & set(open_origins)
    for name in set(c_o) | (cleared - set(c_o)):
        if name in skipped or name in keep:
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


def checkpoint(params: dict, budget_s: float | None = None) -> dict:
    """Save or load state for the already-connected named browser daemon session, within
    OPERATION_TIMEOUT_S or ``budget_s`` if shorter (a Mac sync's steps share one budget)."""
    if not isinstance(params, dict):
        raise ValueError("browser state params are required")
    action = params.get("action")
    if action not in ("save", "load", "base"):
        raise ValueError("browser state action must be save, load or base")
    source = params.get("source", "cloud")
    if source not in ("cloud", "mac"):
        raise ValueError("browser state source must be cloud or mac")
    mac = source == "mac"
    cdp_url = _valid_cdp_url(params.get("cdpUrl"), source)
    state_url = _valid_url(params.get("stateUrl"), "https")
    previous_url = params.get("previousStateUrl")
    for extra in ("baseOutUrl", "baseStateUrl"):
        if params.get(extra) is not None:
            _valid_url(params[extra], "https")
    path: str | None = None
    with _operation_env(cdp_url) as env:
        deadline = time.monotonic() + (OPERATION_TIMEOUT_S if budget_s is None else min(OPERATION_TIMEOUT_S, budget_s))
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
                if mac:
                    exported = _vet(exported)[0]
                exported["visibleOrigins"] = sorted(_by_origin(exported))
                _upload(state_url, json.dumps(exported, separators=(",", ":")).encode(), deadline)
                return {"ok": True, "bytes": 0, "base": True}
            if action == "save":
                _run(env, "connect", cdp_url, deadline=deadline)
                open_origins = _open_origins(env, deadline) if params.get("baseOutUrl") else set()
                _run(env, "state", "save", path, deadline=deadline)
                current = _valid_state(Path(path).read_bytes())
                read = _closed_origins(env, cdp_url, mac, current, open_origins, deadline)
                body = json.dumps(current, separators=(",", ":")).encode()
                keep: set[str] = set()
                if mac:  # never the raw export: only its vetted re-encoding is uploaded
                    current, keep = _vet(current)
                    body = json.dumps(current, separators=(",", ":")).encode()
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
                        _merge_three(base, current, previous, open_origins, keep=keep, hold_back=mac),
                        separators=(",", ":")
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
                    _forget_committed(cdp_url if mac else None, read, previous)
                    return {"ok": True, "bytes": len(merged), "base": True}
                # This fresh client exports its open pages plus the closed origins it read;
                # merging retains prior checkpoints.
                previous = _EMPTY_STATE
                if previous_url:
                    previous = _valid_state(_download(_valid_url(previous_url, "https"), deadline))
                    current = _merge_state(current, previous)
                    body = json.dumps(current, separators=(",", ":")).encode()
                    if len(body) > MAX_STATE_BYTES:
                        raise ValueError("browser state is too large")
                _upload(state_url, body, deadline)
                _forget_committed(cdp_url if mac else None, read, previous)
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
                if mac:  # no navigation on the person's screen: a background tab, answered locally
                    good = [saved[o] for o in origins if _good_storage(saved[o].get("localStorage"))]
                    try:
                        reached = _mac_origins(cdp_url, good, origins_deadline)
                    except Exception:  # noqa: BLE001 -- the sign-ins (cookies) are loaded already
                        reached = set()
                for origin in [] if mac else origins:
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
                if not mac and deadline - time.monotonic() >= RELOAD_MIN_S:
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
                if mac:
                    exported = _vet(exported)[0]
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
