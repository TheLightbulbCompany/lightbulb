"""Hermes's browser vault, backed by the workspace's website logins.

Hermes ships a model-blind vault (``agent/vault_store.py`` + ``tools/browser_vault_tool.py``):
the model only ever sees opaque handles, and ``browser_vault_fill`` resolves the password
server-side and types it into the page over the supervisor's CDP socket, never into a tool
result, a log or the session DB. We keep all of that. Two things we replace:

* **The store.** Hermes's local store keeps its Fernet key BESIDE the ciphertext under
  ``$HERMES_HOME/vault/``, which our profile sync would carry to S3, and it is per profile while
  a workspace's logins are shared by all of its agents. ``install()`` points Hermes's one store
  seam (``agent.vault_store.get_vault_store``, which ``LocalLoginBackend`` and
  ``browser_vault_save_login`` both late-bind) at ``WorkspaceVault``: metadata from the backend,
  one password per fill, held in memory for that fill only. Nothing is written to the profile.
* **The asks.** ``browser_vault_save_login`` / ``browser_vault_enter_code`` ask "the user in
  their UI" through per-thread prompt callbacks, which only Hermes's own CLI/desktop install.
  ``prompt_scope`` installs ours for a turn. A LOGIN ask becomes a Needs-you request in the room
  and Activity, and the callback waits for the human's answer (``WAIT_S``), which goes from their
  form straight to the backend store -- never through chat. A CODE is different (owner ruling
  2026-10-07): nobody types it into a form for the agent. The callback posts one line sending the
  person to the shared browser, waits for the page to move on (they typed the code into the live
  view themselves), and answers Hermes with "" either way; ``transform_tool_result`` then turns
  Hermes's ``code_declined`` into what actually happened. The code never passes through chat, the
  model, or our storage.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar

import bridge
from bridge import API, browser_control

log = logging.getLogger(__name__)

# Under Hermes's 420 s sequential tool deadline (agent/tool_executor.py): past it the model
# gets tool_timeout while this worker would keep waiting -- and could consume a code nobody reads.
WAIT_S = 390
POLL_S = 2.0
RENEW_S = 60.0  # well inside the backend's 300 s agent lease
_ASK: ContextVar[dict | None] = ContextVar("website_login_ask", default=None)
_install_lock = threading.Lock()
MANAGED_HERE = "Connections → Website sign-ins"
# Hermes's own copy points at surfaces that do not exist here (its desktop app and CLI).
# Ordered: a longer phrase is replaced before any phrase it contains.
_FOREIGN_POINTERS = (
    ("run `hermes vault add` in a terminal, or in the desktop app open Settings → Credential Vault",
     f"a workspace admin adds it in {MANAGED_HERE}"),
    ("run `hermes vault add` or use Desktop → Settings → Passwords & Logins", f"add it in {MANAGED_HERE}"),
    ("Settings → Passwords & Logins / `hermes vault add`", MANAGED_HERE),
    ("Desktop → Settings → Passwords & Logins", MANAGED_HERE),
    ("Settings → Passwords & Logins", MANAGED_HERE),
    ("`hermes vault add`", MANAGED_HERE),
)


def _call(method: str, path: str, body: dict | None = None, timeout: float = 20.0) -> dict | None:
    """One backend call under the vault token (never the hook token the agent's shell holds). 404 → None; anything else raises (never with a secret:
    the body is only ever sent, and error text is the status line)."""
    request = urllib.request.Request(
        f"{API}/api/v1/internal/website-logins/{bridge.OWNER}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {bridge.VAULT_TOKEN}"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"website logins: HTTP {exc.code}") from None


def _error(message: str) -> Exception:
    try:
        from agent.vault_store import VaultError

        return VaultError(message)
    except ImportError:  # bridge-only unit tests
        return RuntimeError(message)


def _allowed(origin: str) -> tuple:
    """The saved origin plus its www/apex twin: an admin who types supademo.com means the site,
    and Hermes compares origins exactly. Nothing broader is ever inferred."""
    scheme, _, host = origin.partition("://")
    twin = host[4:] if host.startswith("www.") else f"www.{host}"
    return (origin, f"{scheme}://{twin}")


def _meta(row: dict):
    from agent.vault_store import VaultItemMeta

    username = str(row.get("username") or "")
    kind = "email" if "@" in username else ("phone" if username.lstrip("+").isdigit() else "username")
    origin = str(row.get("origin") or "")
    return VaultItemMeta(
        id=str(row["id"]), kind="login", label=origin.split("://", 1)[-1], origin=origin,
        created_at=str(row.get("createdAt") or ""), identifier_type=kind, identifier=username or None,
        allowed_origins=_allowed(origin),
    )


class WorkspaceVault:
    """Stands in for ``agent.vault_store.VaultStore`` (the methods Hermes calls on it)."""

    def list_items(self):
        rows = (_call("GET", "") or {}).get("logins") or []
        return [_meta(row) for row in rows if row.get("id") and row.get("origin")]

    def has_items(self) -> bool:
        try:
            return bool(self.list_items())
        except Exception:  # noqa: BLE001 -- Hermes's own has_items never raises
            return False

    def get_meta(self, item_id: str):
        return next((m for m in self.list_items() if m.id == item_id), None)

    def resolve_secret(self, item_id: str) -> dict:
        ask = _ASK.get() or {}
        found = _call("POST", f"/logins/{urllib.parse.quote(item_id, safe='')}/resolve",
                      {"agentId": ask.get("agentId", "")})
        if not found or not found.get("password"):
            raise _error(f"no website login with id {item_id!r}")
        return {"password": found["password"]}

    def add_item(self, kind: str, label: str, secret: dict, origin: str | None = None):
        """Only reached by ``browser_vault_save_login`` after our prompt answered: the human's form
        already saved the login, so this finds it. Anything else is refused -- the model never
        holds a password, so it can never have one to add."""
        if kind != "login":
            raise _error("Only website logins can be saved here.")
        from agent.vault_store import normalize_origin

        wanted = (normalize_origin(origin or ""), str(secret.get("identifier") or "").strip())
        found = next((m for m in self.list_items() if (m.origin, m.identifier) == wanted), None)
        if found is None:
            raise _error(f"That login was not saved. Ask again with browser_vault_save_login, or add it in {MANAGED_HERE}.")
        return found

    def remove_item(self, item_id: str) -> bool:
        raise _error(f"Website sign-ins are managed by workspace admins in {MANAGED_HERE}.")


def install() -> None:
    """Point Hermes's vault at the workspace store, once per process."""
    try:
        import agent.vault_store as vault_store
    except ImportError:  # bridge-only unit tests
        return
    with _install_lock:
        if getattr(vault_store.get_vault_store, "_lightbulb", False):
            return
        factory = WorkspaceVault
        factory._lightbulb = True
        vault_store.get_vault_store = factory
        try:
            from tools import browser_vault_tool as tool

            schema = tool.BROWSER_VAULT_SAVE_LOGIN_SCHEMA
            schema["description"] = rewrite_pointers(schema["description"])
        except ImportError:
            pass


def rewrite_pointers(text: str) -> str:
    for foreign, ours in _FOREIGN_POINTERS:
        # Tool results are json.dumps(ensure_ascii=True): the arrow arrives escaped there.
        text = text.replace(foreign, ours).replace(json.dumps(foreign)[1:-1], json.dumps(ours)[1:-1])
    return text


# ---- asks -------------------------------------------------------------------------------------


def _ask(kind: str, origin: str) -> dict | None:
    """File one ask and wait for its answer. None = declined, expired, aborted or unreachable."""
    ctx = _ASK.get()
    if not ctx:
        return None
    try:
        created = _call("POST", "/requests", {
            "kind": kind, "origin": origin, "agentId": ctx["agentId"],
            "roomId": ctx.get("roomId"), "threadRootMessageId": ctx.get("threadRootMessageId"),
        })
    except Exception:  # noqa: BLE001
        log.warning("website login ask could not be filed")
        return None
    request_id = str((created or {}).get("requestId") or "")
    if not request_id:
        return None
    deadline = time.monotonic() + WAIT_S
    renewed = time.monotonic()
    while time.monotonic() < deadline:
        if time.monotonic() - renewed > RENEW_S:
            browser_control.renew()
            renewed = time.monotonic()
        if ctx.get("done") or ctx.get("aborted", lambda: False)():
            break
        try:
            state = _call("GET", f"/requests/{request_id}") or {"status": "expired"}
        except Exception:  # noqa: BLE001 -- a blip; keep waiting
            state = {"status": "pending"}
        if state.get("status") != "pending":
            return state if state.get("status") == "answered" else None
        time.sleep(POLL_S)
    try:
        _call("POST", f"/requests/{request_id}/cancel", {})
    except Exception:  # noqa: BLE001
        pass
    return None


def _save_login_prompt(origin: str, _site: str) -> dict | None:
    state = _ask("login", origin)
    if not state or not state.get("loginId"):
        return None
    found = _call("POST", f"/logins/{urllib.parse.quote(str(state['loginId']), safe='')}/resolve",
                  {"agentId": (_ASK.get() or {}).get("agentId", "")})
    if not found or not found.get("password"):
        return None
    return {"identifier": found.get("username", ""), "password": found["password"]}


def _browser_task_ids() -> list[str]:
    """The Hermes browser tasks this run has browsed on (``browser_control`` records them per run):
    the main agent's, plus one per delegated subagent that browsed. Hermes's code prompt does not
    say which task is asking, so the page watch covers all of them."""
    state = browser_control._TURN.get() or {}
    tasks = browser_control._BROWSING.get(str(state.get("runId") or ""), ())
    return sorted(tasks) or ["default"]


def _browsing_on_mac() -> bool:
    """A run that selected a paired Mac browses in that Mac's "Lightbulb agent" Chrome, not the
    cloud browser: the person types the code into that window, and the Browser panel shows nothing."""
    return bool((browser_control._TURN.get() or {}).get("mac"))


def _page_eval(task_id: str, expression: str):
    """Evaluate page JS on one task's tab through Hermes's own (non-secret) eval path. None = no answer."""
    try:
        from tools.browser_vault_tool import _eval_js
    except ImportError:  # bridge-only unit tests
        return None
    try:
        out = _eval_js(task_id, expression)
    except Exception:  # noqa: BLE001 -- the page is a stranger; a blip is not an answer
        return None
    return out.get("result") if isinstance(out, dict) and out.get("success") else None


def _page_heading() -> str:
    """The first heading among the run's tabs ("Check your text messages"), for the line in the room."""
    for task_id in _browser_task_ids():
        text = " ".join(str(_page_eval(task_id, "(document.querySelector('h1,h2')||{}).innerText||''") or "").split())
        if text:
            return text[:120]
    return ""


def _code_field_present() -> bool | None:
    """Whether any of the run's tabs still shows a one-time-code field (Hermes's own OTP probe).
    The code page is in exactly one of them, so the step has moved on only when EVERY tab answered
    and none shows a field; a tab that did not answer may be the one with the field, so that is
    None (keep waiting), never False."""
    try:
        from tools.browser_vault_tool import _TAB_PROBES
    except ImportError:  # bridge-only unit tests
        return None
    unknown = False
    for task_id in _browser_task_ids():
        result = _page_eval(task_id, _TAB_PROBES["otp"])
        if result is None:
            unknown = True
        elif result is True or str(result).strip().lower() == "true":
            return True
    return None if unknown else False


def _code_prompt(site: str, _hint: str) -> str:
    """Hermes asks us for the one-time code. Nobody types it here: post one line sending the person
    to the shared browser, then wait for the page to move on (they typed it into the live view) or
    for the deadline. Always "" -- Hermes then reports ``code_declined``, and ``transform_tool_result``
    tells the agent what happened (``ctx["code"]``: ``page_moved`` or ``nobody_came``)."""
    ctx = _ASK.get()
    if not ctx:
        return ""
    if not ctx.get("roomId"):
        # A Paperclip or voice turn has no room to say it in: nobody would be told, so do not wait.
        ctx["code"] = "no_room"
        return ""
    try:
        posted = _call("POST", "/requests", {
            "kind": "code", "origin": site if "://" in site else f"https://{site}",
            "agentId": ctx["agentId"], "roomId": ctx.get("roomId"),
            "threadRootMessageId": ctx.get("threadRootMessageId"), "hint": _page_heading(),
            "surface": "mac" if _browsing_on_mac() else "cloud",
        })
    except Exception:  # noqa: BLE001
        posted = None
    if (posted or {}).get("status") != "posted":
        # Nobody was told (the room write failed, or the backend is older): waiting would be for
        # a person who never heard. The agent says the line itself and ends its turn.
        log.warning("website login code notice was not posted")
        ctx["code"] = "not_told"
        return ""
    ctx["code"] = "nobody_came"
    deadline = time.monotonic() + WAIT_S
    renewed = time.monotonic()
    while time.monotonic() < deadline:
        if time.monotonic() - renewed > RENEW_S:
            browser_control.renew()
            renewed = time.monotonic()
        if ctx.get("done") or ctx.get("aborted", lambda: False)():
            break
        if _code_field_present() is False:
            ctx["code"] = "page_moved"
            break
        time.sleep(POLL_S)
    return ""


# What the agent reads instead of Hermes's "The user did not enter a code" (which assumes a form).
CODE_RESULTS = {
    "page_moved": {
        "success": True, "filled_fields": 0, "source": "person",
        "next": "The person completed this step in the shared browser. Take a fresh browser_snapshot "
                "and continue from what the page shows now. Do not ask for the code.",
    },
    "nobody_came": {
        "success": False, "error_type": "nobody_came",
        "error": "Nobody typed the code into the shared browser in time. Say so in one line and stop: "
                 "they can type it any time (Browser panel, on a computer) and tell you to continue, "
                 "and you pick up from a fresh browser_snapshot. Never ask for the code in chat, and "
                 "never ask again for a login that is already saved.",
    },
    "not_told": {
        "success": False, "error_type": "not_told",
        "error": "The line asking the person to type the code could not be posted, so nobody has been "
                 "told. Say it yourself in one line -- the site is asking for a verification code; type "
                 "it into the shared browser (Browser panel, on a computer) and tell me to continue -- "
                 "then end your turn and pick up from a fresh browser_snapshot. Never ask for the code "
                 "in chat.",
    },
    "no_room": {
        "success": False, "error_type": "no_room",
        "error": "This site wants a verification code and this run has no room to ask a person in. "
                 "Stop here and say so in your result: someone has to sign in to the site once in the "
                 "shared browser (Browser panel), after which the saved session carries over. Never ask "
                 "for the code in chat.",
    },
}


@contextmanager
def prompt_scope(agent_id: str, room_id: str | None, thread_root: str | None, aborted=None):
    """Let this turn's vault tools ask a human. Thread-local callbacks (Hermes propagates them to
    its tool workers); the unlock prompt is installed only because ``can_prompt_here`` keys off
    it -- no password manager is installed in this image, so it never actually asks."""
    install()
    try:
        from agent.vault_backends import unlock
    except ImportError:
        yield
        return
    ask = {"agentId": agent_id, "roomId": room_id, "threadRootMessageId": thread_root,
           "aborted": aborted or (lambda: False), "done": False}
    token = _ASK.set(ask)
    unlock.set_unlock_prompt_callback(lambda *_a: "")
    unlock.set_save_login_prompt_callback(_save_login_prompt)
    unlock.set_code_prompt_callback(_code_prompt)
    try:
        yield
    finally:
        ask["done"] = True  # a tool worker still waiting after the turn stops at its next poll
        unlock.set_unlock_prompt_callback(None)
        unlock.set_save_login_prompt_callback(None)
        unlock.set_code_prompt_callback(None)
        _ASK.reset(token)


def transform_tool_result(tool_name: str = "", result=None, **_kw):
    """Plugin hook: Hermes's vault errors name its desktop app and CLI; ours is Connections. And
    its ``code_declined`` ("the user did not enter a code") assumes a form nobody has here: it
    becomes the outcome ``_code_prompt`` recorded for this turn."""
    if not (str(tool_name).startswith("browser_vault_") and isinstance(result, str)):
        return None
    rewritten = rewrite_pointers(result)
    if tool_name == "browser_vault_enter_code" and '"code_declined"' in rewritten:
        outcome = (_ASK.get() or {}).get("code")
        if outcome in CODE_RESULTS:
            rewritten = json.dumps(CODE_RESULTS[outcome])
    return rewritten if rewritten != result else None
