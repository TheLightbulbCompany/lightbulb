"""Brokered credentials for one turn (spec 13 §4.2, lane V11).

The backend holds every rotating grant it custodies (ChatGPT login, X, QuickBooks, Instagram:
rows with ``custody: backend``) and vends short-lived access tokens; a VM never refreshes one
itself and never writes one back. Per turn, `vend` asks the existing credentials route, maps the
MCP bearers as before (`turn.runtime_mcp_env` keeps its name and shape), writes the vended ChatGPT
token into the ROOT ``auth.json`` beside a placeholder refresh token, and patches the three CLI
files the backend materialized token-free. Hermes's own refresh paths (F6:
``hermes_cli.auth_codex.set_refresh_delegate``) end in `codex_refresh_delegate`, one forced
refresh at the broker, registered only once a vend has answered with ``model``. `TurnRenewer`
re-vends a long turn before anything it holds expires.

Inert without custody: a vend for a workspace with no backend-custodied row carries no ``model``
and none of the three CLI keys, nothing here branches, and the delegate is never registered.
Logs carry ids and numbers, never a token.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bridge import API, HOOK_TOKEN, OWNER, sync

log = logging.getLogger("bridge.credentials")

PLACEHOLDER_REFRESH = "lightbulb-brokered"
MODEL_KEY = "model"
RENEW_WINDOW_S = 600  # re-vend when a brokered grant the turn holds expires within 10 min
RENEW_WAKE_S = 60.0
VEND_TIMEOUT_S = 15
MODEL_TIMEOUT_S = 40  # the broker holds its lease up to 30 s before answering 409 busy

#: vend key -> the CLI file (relative to the profile dir) the backend materialized token-free
CLI_FILES = {
    "x": f"{sync.PROFILE_HOME}/.xurl",
    "quickbooks": f"{sync.PROFILE_HOME}/quickbooks/.env",
    "instagram": "skills/instagram/credentials.json",
}
_XURL_APP = "lightbulb-x"  # materializers/xurl_cli._XURL_APP_NAME


@dataclass
class Vend:
    tokens: dict[str, str]
    expires_at: dict[str, int] = field(default_factory=dict)  # vend key -> epoch seconds
    model: dict | None = None  # {"access_token", "id_token", "account_id", "expires_at"} under backend custody

    def mcp_env(self) -> dict[str, str]:
        """The turn's MCP bearers, exactly as `turn.runtime_mcp_env` always mapped them."""
        return {
            f"MCP_{re.sub(r'[^A-Za-z0-9]', '_', key).upper()}_ACCESS_TOKEN": token
            for key, token in self.tokens.items()
            if re.fullmatch(r"[A-Za-z0-9_-]+", key)
        }

    @property
    def brokered(self) -> set[str]:
        """What the backend custodies for this workspace: ``model`` and the CLI keys present."""
        return ({MODEL_KEY} if self.model else set()) | (set(self.tokens) & set(CLI_FILES))

    def next_expiry(self) -> int | None:
        due = [self.expires_at[k] for k in self.brokered if self.expires_at.get(k)]
        return min(due) if due else None


_LAST: dict[str, Vend] = {}  # agent -> its last vend: the renewer's clock
_AGENT: str | None = None  # the agent whose vend registered the delegate: the /model body's agent_id
_DELEGATE_ON = False


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(raw: bytes) -> dict:
    try:
        out = json.loads(raw)
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _post(path: str, body: dict, timeout: float, *, api: str, hook_token: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{api}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {hook_token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200), _json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read())


def vend(agent_id: str, *, api: str | None = None, owner: str | None = None, hook_token: str | None = None) -> Vend | None:
    """One vend for a turn: the MCP bearers, with the brokered grants applied locally on the way.
    None when the route is unreachable or answers nonsense (the turn goes on without them)."""
    api, owner, hook_token = api or API, owner or OWNER, hook_token or HOOK_TOKEN
    if not api or not owner or not hook_token:
        return None
    try:
        status, payload = _post(f"/api/v1/runtime/credentials/u/{owner}", {"agent_id": agent_id}, VEND_TIMEOUT_S, api=api, hook_token=hook_token)
    except Exception:  # noqa: BLE001 -- a missing optional integration must not fail the turn
        log.warning("brokered MCP credentials unavailable for agent=%s", agent_id)
        return None
    tokens = payload.get("tokens")
    if status != 200 or not isinstance(tokens, dict):
        log.warning("brokered MCP credentials returned an invalid payload for agent=%s", agent_id)
        return None
    expires = payload.get("expires_at") if isinstance(payload.get("expires_at"), dict) else {}
    model = payload.get("model")
    out = Vend(
        tokens={k: t for k, t in tokens.items() if isinstance(k, str) and isinstance(t, str) and t},
        expires_at={k: int(v) for k, v in expires.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0},
        model=model if isinstance(model, dict) and isinstance(model.get("access_token"), str) and model["access_token"] else None,
    )
    apply(agent_id, out)
    return out


def apply(agent_id: str, vended: Vend) -> None:
    """Fill the brokered grants in locally. Never raises."""
    if vended.model:
        write_codex_auth(vended.model)
    _set_delegate(vended.model is not None, agent_id)
    apply_cli_tokens(agent_id, vended.tokens, vended.expires_at)
    _LAST[agent_id] = vended


# ---- ChatGPT login: the root auth.json ------------------------------------------------------------
def write_codex_auth(model: dict) -> None:
    """The ROOT ``auth.json`` as Hermes reads it, with the vended access token and the placeholder
    refresh token -- never a real one, so nothing on this VM can spend the grant. Rewritten only
    on a change (the renewer and the delegate call this too)."""
    path = sync.HOME / "auth.json"
    blob = _json(path.read_bytes()) if path.is_file() else {}
    providers = blob.get("providers") if isinstance(blob.get("providers"), dict) else {}
    state = providers.get("openai-codex") if isinstance(providers.get("openai-codex"), dict) else {}
    tokens = {"access_token": model["access_token"], "refresh_token": PLACEHOLDER_REFRESH}
    for key in ("id_token", "account_id"):
        if isinstance(model.get(key), str) and model[key]:
            tokens[key] = model[key]
    if state.get("tokens") == tokens and state.get("custody") == "backend":
        sync._CODEX_BROKERED = True
        return
    providers["openai-codex"] = {
        **state,
        "custody": "backend",
        "tokens": tokens,
        "last_refresh": _now_z(),
        "auth_mode": state.get("auth_mode") or "chatgpt",
    }
    blob.update({"version": blob.get("version") or 1, "active_provider": "openai-codex", "providers": providers})
    _atomic_write(path, json.dumps(blob, indent=2).encode(), mode=0o600)
    sync._CODEX_BROKERED = True


class BrokerRefreshError(RuntimeError):
    """Outside the Hermes image (dev, tests): the fields Hermes's AuthError carries."""

    def __init__(self, message: str, code: str, relogin: bool):
        super().__init__(message)
        self.code, self.relogin_required = code, relogin


def _auth_error(message: str, code: str, *, relogin: bool) -> Exception:
    try:
        from hermes_cli.auth_codex import _codex_err
    except ImportError:
        return BrokerRefreshError(message, code, relogin)
    return _codex_err(message, code, relogin=relogin)


def _set_delegate(on: bool, agent_id: str) -> None:
    """F6 registration: only after a vend returned ``model``; off again when a later vend does not
    (custody rolled back to the VM: Hermes refreshes with the real refresh token it now holds)."""
    global _DELEGATE_ON, _AGENT
    if on:
        _AGENT = agent_id
    if on == _DELEGATE_ON:
        return
    try:
        from hermes_cli.auth_codex import set_refresh_delegate
    except ImportError:
        return  # outside the Hermes image: nothing to register
    set_refresh_delegate(codex_refresh_delegate if on else None)
    _DELEGATE_ON = on


def codex_refresh_delegate(access_token: str, refresh_token: str) -> dict:
    """F6: Hermes asks for a refresh (a 401 on ``access_token``, or the pool's expiry check). One
    forced refresh at the broker for the token this VM presents; the broker refreshes once for N
    VMs and hands the others the newer token. 410 = the grant is dead (terminal: Hermes quarantines
    the entry, the owner reconnects); anything else is transient, never a dead grant."""
    agent_id = _AGENT or ""
    body = {"agent_id": agent_id, "presented_sha256": hashlib.sha256(access_token.encode()).hexdigest(), "force": True}
    try:
        status, payload = _post(f"/api/v1/runtime/credentials/u/{OWNER}/model", body, MODEL_TIMEOUT_S, api=API, hook_token=HOOK_TOKEN)
    except Exception as exc:  # noqa: BLE001 -- unreachable/timeout: transient
        raise _auth_error("Lightbulb token broker unreachable", "lightbulb_broker_busy", relogin=False) from exc
    if status == 410:
        raise _auth_error("ChatGPT login needs reconnecting", "invalid_grant", relogin=True)
    model = payload.get("model")
    if status != 200 or not isinstance(model, dict) or not isinstance(model.get("access_token"), str) or not model["access_token"]:
        raise _auth_error(f"Lightbulb token broker answered {status}", "lightbulb_broker_busy", relogin=False)
    log.info("codex refresh via broker refreshed=%s agent=%s", bool(payload.get("refreshed")), agent_id)
    write_codex_auth(model)
    if (last := _LAST.get(agent_id)) and isinstance(model.get("expires_at"), int) and model["expires_at"] > 0:
        last.expires_at[MODEL_KEY] = model["expires_at"]
    return {"access_token": model["access_token"], "refresh_token": PLACEHOLDER_REFRESH, "last_refresh": _now_z()}


# ---- X / QuickBooks / Instagram: the token-free CLI files ---------------------------------------
def apply_cli_tokens(agent_id: str, tokens: dict[str, str], expires_at: dict[str, int]) -> list[str]:
    """Patch the vended access token (and its expiry) into each CLI file the backend custodies for
    this workspace, in EVERY local profile: the grant is owner-level, and xurl rides the ``$HOME``
    of whichever profile first ran a shell (sync.save_rotated_cli_credentials), not necessarily
    ``agent_id``'s. Local only: each file is marked brokered so `sync.save_rotated_cli_credentials`
    never reads the patch as a rotation to write back. Returns the agent-relative paths patched."""
    profiles = sync.HOME / "profiles"
    agents = sorted({agent_id, *(p.name for p in profiles.iterdir() if p.is_dir())} if profiles.is_dir() else {agent_id})
    done: list[str] = []
    for key, rel in CLI_FILES.items():
        token = tokens.get(key)
        if not token:
            continue
        for agent in agents:
            path = sync._profile_dir(agent) / rel
            if not path.is_file():
                continue  # not materialized here (not connected, or the pull is still due): nothing to fill
            try:
                raw = path.read_bytes()
                new = _PATCHERS[key](raw, token, int(expires_at.get(key) or 0))
                if new != raw:
                    _atomic_write(path, new, like=path)
            except Exception:  # noqa: BLE001 -- the CLI then fails as it does today with a stale token
                log.warning("brokered %s token not written for agent=%s", key, agent, exc_info=True)
                continue
            sync.mark_brokered(agent, rel)
            done.append(f"{agent}/{rel}")
    return done


def _patch_xurl(raw: bytes, token: str, exp: int) -> bytes:
    import yaml

    data = yaml.safe_load(raw.decode("utf-8")) or {}
    app = data["apps"][_XURL_APP]
    user = app.get("default_user") or next(iter(app.get("oauth2_tokens") or {}), None)
    if not user:
        raise ValueError("xurl app has no user")
    oauth2 = app.setdefault("oauth2_tokens", {}).setdefault(user, {"type": "oauth2"}).setdefault("oauth2", {})
    oauth2.update({"access_token": token, "refresh_token": "", "expiration_time": exp})
    return yaml.safe_dump(data, sort_keys=False).encode("utf-8")


def _patch_qbo_env(raw: bytes, token: str, exp: int) -> bytes:
    lines = [ln for ln in raw.decode("utf-8").splitlines() if not ln.startswith(("QUICKBOOKS_ACCESS_TOKEN=", "QUICKBOOKS_ACCESS_TOKEN_EXPIRES_AT="))]
    lines.append(f"QUICKBOOKS_ACCESS_TOKEN={token}")
    if exp:
        lines.append(f"QUICKBOOKS_ACCESS_TOKEN_EXPIRES_AT={exp}")  # epoch seconds (the server's ms guard tolerates ms)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _patch_instagram(raw: bytes, token: str, exp: int) -> bytes:
    creds = json.loads(raw.decode("utf-8"))
    expiry = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if exp else ""
    creds.update({"access_token": token, "token_expiry": expiry, "custody": "backend"})
    return json.dumps(creds, indent=2).encode("utf-8")


_PATCHERS = {"x": _patch_xurl, "quickbooks": _patch_qbo_env, "instagram": _patch_instagram}


def _atomic_write(path: Path, data: bytes, *, mode: int | None = None, like: Path | None = None) -> None:
    """Write beside and rename over: a CLI reading mid-write sees the old file or the new one."""
    st = like.stat() if like is not None and like.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, st.st_mode & 0o7777 if st else (mode if mode is not None else 0o600))
        if st is not None:
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except (PermissionError, OSError):
                pass
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---- long turns ---------------------------------------------------------------------------------
class TurnRenewer:
    """A daemon per turn scope: wakes every 60 s and re-vends when a brokered grant the turn holds
    expires within 10 min, so a long turn's ChatGPT token and CLI files stay fresh (Hermes also
    refreshes on a 401 through the delegate; the CLIs cannot). Stopped with the scope.
    ponytail: N live turns of one agent = N renewers; each extra vend is one lease-free read at the
    backend. One renewer per agent if that ever shows in the vend metric."""

    def __init__(self, agent_id: str, *, wake_s: float | None = None, clock=time.time, revend=None):
        self.agent_id = agent_id
        self._wake = RENEW_WAKE_S if wake_s is None else wake_s
        self._clock, self._revend = clock, revend or vend
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"renew-{agent_id[:8]}", daemon=True)

    @classmethod
    def for_turn(cls, agent_id: str) -> TurnRenewer | None:
        """Started only for a turn whose vend holds something brokered: inert elsewhere."""
        last = _LAST.get(agent_id)
        return cls(agent_id).start() if last is not None and last.brokered else None

    def start(self) -> TurnRenewer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def due(self) -> bool:
        last = _LAST.get(self.agent_id)
        nxt = last.next_expiry() if last is not None else None
        return nxt is not None and nxt - self._clock() < RENEW_WINDOW_S

    def tick(self) -> bool:
        if not self.due():
            return False
        self._revend(self.agent_id)
        return True

    def _run(self) -> None:
        while not self._stop.wait(self._wake):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- the next wake tries again; Hermes's 401 path is the backstop
                log.warning("turn renewer failed for agent=%s", self.agent_id, exc_info=True)
