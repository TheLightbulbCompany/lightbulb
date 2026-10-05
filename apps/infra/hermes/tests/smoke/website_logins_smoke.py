"""Prove the pinned Hermes vault reads the workspace store once the bridge installs it: the
real ``browser_vault_list`` / ``LocalLoginBackend`` / save-login seam, with the backend faked.
No password may appear in any tool result, and nothing may be written under the profile."""

import json
import os
import tempfile
from pathlib import Path

PASSWORD = "smoke-Pa55word-not-real"
LOGIN = {"id": "vault_0123456789ab", "origin": "https://app.example.com", "username": "me@example.com",
         "createdAt": "2026-09-25T00:00:00Z"}

with tempfile.TemporaryDirectory() as root:
    os.environ["HERMES_HOME"] = root
    from bridge import website_logins

    calls = []

    def fake_call(method, path, body=None, timeout=20.0):
        calls.append((method, path))
        if method == "GET" and path == "":
            return {"logins": [LOGIN]}
        if path == f"/logins/{LOGIN['id']}/resolve":
            return {"username": LOGIN["username"], "password": PASSWORD}
        if path == "/requests":
            return {"requestId": "r" * 32}
        if path.startswith("/requests/"):
            return {"status": "answered", "loginId": LOGIN["id"]}
        return None

    website_logins._call = fake_call
    website_logins.POLL_S = 0
    website_logins.install()

    import agent.vault_store as vault_store
    from agent.vault_backends import backend_for_handle
    from agent.vault_backends.unlock import can_prompt_here, get_save_login_prompt_callback
    from tools import browser_vault_tool as tool

    assert isinstance(vault_store.get_vault_store(), website_logins.WorkspaceVault)
    assert "Passwords & Logins" not in tool.BROWSER_VAULT_SAVE_LOGIN_SCHEMA["description"]

    listed = json.loads(tool.browser_vault_list())
    assert listed["items"][0]["handle"] == LOGIN["id"] and listed["items"][0]["identifier"] == LOGIN["username"]
    backend = backend_for_handle(LOGIN["id"])
    assert backend.name == "local" and backend.resolve_password(LOGIN["id"]) == PASSWORD

    missing = tool.browser_vault_fill("vault_ffffffffffff")
    rewritten = website_logins.transform_tool_result("browser_vault_fill", missing)
    assert "hermes vault add" not in rewritten and "Website sign-ins" in rewritten, rewritten

    assert not can_prompt_here()
    with website_logins.prompt_scope("agent-1", None, None):
        assert can_prompt_here()
        answer = get_save_login_prompt_callback()("https://app.example.com", "app.example.com")
        assert answer == {"identifier": LOGIN["username"], "password": PASSWORD}
        meta = vault_store.get_vault_store().add_item(
            "login", "app.example.com", {"identifier_type": "email", **answer}, origin="https://app.example.com")
        assert meta.id == LOGIN["id"]
    assert get_save_login_prompt_callback() is None

    for out in (json.dumps(listed), missing, rewritten):
        assert PASSWORD not in out
    assert not (Path(root) / "vault").exists(), sorted(str(p) for p in Path(root).rglob("*"))
print("website logins smoke ok")
