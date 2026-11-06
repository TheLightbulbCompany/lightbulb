"""ISOA-837 (codex_pool_refresh_smoke.py) in broker mode (spec 13 §8.3): the root auth.json is the
backend's marker filled with a vended token and the placeholder refresh token, F6's delegate is
registered by a vend that returned ``model``. Three in-flight turns hit a 401 on the same expired
token: all three recover, and the broker is asked to refresh once per presented token -- the fake
broker refreshes only when the presented hash is the current token (the backend's rule), so the
VM never spends a real refresh token. Runs Hermes's real pool code in the pinned image; only the
bridge's HTTP POST is replaced."""

import base64
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from bridge import credentials, sync, turn


def jwt(tag: str, exp: int) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp, 'tag': tag})}.sig"


def tag(token: str) -> str:
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))["tag"]


now = int(time.time())
broker = {"current": jwt("A1", now + 3600), "n": 1, "refreshes": 0, "presented": []}


def fake_post(path, body, timeout, *, api, hook_token):
    if path.endswith("/model"):
        broker["presented"].append(body["presented_sha256"])
        if body["force"] and body["presented_sha256"] == hashlib.sha256(broker["current"].encode()).hexdigest():
            broker["n"] += 1
            broker["refreshes"] += 1
            broker["current"] = jwt(f"A{broker['n']}", now + 7200)
        return 200, {"model": {"access_token": broker["current"], "expires_at": now + 7200}, "refreshed": True}
    return 200, {"tokens": {}, "model": {"access_token": broker["current"], "expires_at": now + 3600}}


credentials._post = fake_post
credentials.API, credentials.OWNER, credentials.HOOK_TOKEN = "https://api.invalid", "org_smoke", "hook"
with tempfile.TemporaryDirectory() as root:
    os.environ["HERMES_HOME"] = root
    sync.HOME = Path(root)
    sync._profile_dir("ceo").mkdir(parents=True)
    root_auth = Path(root) / "auth.json"
    root_auth.write_text(json.dumps({"providers": {"openai-codex": {"custody": "backend", "tokens": {}}},
                                     "active_provider": "openai-codex"}))

    stale = broker["current"]
    assert credentials.vend("ceo") is not None
    import hermes_cli.auth_codex as auth_codex

    assert auth_codex._REFRESH_DELEGATE is credentials.codex_refresh_delegate, "delegate not registered"
    with turn._hermes_home_scope("ceo"), turn._secret_scope("ceo"):
        # Three in-flight turns, all holding the token that just expired. Each turn resolves its
        # own pool (turn._credential_pool), so load all three before any of them refreshes: one
        # shared pool would hold A2 after the first refresh and never match the stale hint again.
        pools = [turn._credential_pool("openai-codex", "gpt-5.6-sol") for _ in range(3)]
        assert all(pools), "no credential pool for openai-codex"
        for pool in pools:
            entry = pool.try_refresh_matching(api_key_hint=stale)
            assert entry is not None and tag(entry.runtime_api_key) != "A1", entry
    # Once per PRESENTED token, not once per stale token: under the auth-store lock each later pool
    # first syncs the peer's newer token from auth.json and force-refreshes THAT, so F6's
    # same-stale-token dedupe never fires on this path and the broker refreshes once per turn.
    presented = broker["presented"]
    assert len(presented) == 3 and len(set(presented)) == 3, broker
    tokens = json.loads(root_auth.read_text())["providers"]["openai-codex"]["tokens"]
    assert tokens["access_token"] == broker["current"] and tokens["refresh_token"] == "lightbulb-brokered", tokens
    assert sync._CODEX_BROKERED
print("ok: three turns recovered through the broker, one refresh per presented token; no real refresh token on the VM")
