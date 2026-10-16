"""Prove a ChatGPT-OAuth turn recovers when its access token expires after ANOTHER turn already
rotated the workspace's grant (prod 2026-10-15 23:17Z, ISOA-837: three in-flight turns died with
"HTTP 401: Provided authentication token is expired").

Hermes's native recovery is the credential pool: on a 401 it refreshes the pool entry that issued
the failing key, first syncing the rotated tokens from the auth.json that owns the grant. The
bridge's agents had no pool, and Hermes's pool-less fallback refuses a rotated token as an
"account swap". This runs Hermes's real pool and auth-store code in the pinned image, in profile
mode, with the grant in the ROOT auth.json exactly as hermes_profiles.stage_codex_auth writes it.
Only the network refresh POST is replaced."""

import base64
import json
import os
import tempfile
import time
from pathlib import Path

from bridge import sync, turn


def jwt(tag: str, exp: int) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp, 'tag': tag})}.sig"


def tag(token: str) -> str:
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))["tag"]


now = int(time.time())
A1, A2 = jwt("A1", now + 3600), jwt("A2", now + 7200)
with tempfile.TemporaryDirectory() as root:
    os.environ["HERMES_HOME"] = root  # the runtime's process home is the root; profiles sit under it
    sync.HOME = Path(root)
    sync._profile_dir("ceo").mkdir(parents=True)
    root_auth = Path(root) / "auth.json"

    def stage(access: str, refresh: str) -> None:
        root_auth.write_text(json.dumps({"version": 1, "active_provider": "openai-codex", "providers": {
            "openai-codex": {"tokens": {"access_token": access, "refresh_token": refresh},
                             "last_refresh": "2026-10-15T22:00:00Z", "auth_mode": "chatgpt"}}}))

    stage(A1, "R1")
    with turn._hermes_home_scope("ceo"), turn._secret_scope("ceo"):  # as in a real turn
        pool = turn._credential_pool("openai-codex", "gpt-5.6-sol")
        assert pool is not None, "no credential pool for openai-codex"
        assert turn._credential_pool("custom", "m") is None

        stage(A2, "R2")  # another turn in this runtime refreshed the grant
        posted = []

        def refresh(*args, **kwargs):
            posted.append(next(v for v in [*args, *kwargs.values()] if isinstance(v, str) and v.startswith("R")))
            return {"access_token": jwt("A3", now + 9000), "refresh_token": "R3",
                    "last_refresh": "2026-10-15T23:18:00Z"}

        import hermes_cli.auth as auth
        import hermes_cli.auth_codex as auth_codex
        from agent import credential_pool
        for module in (auth, auth_codex, credential_pool):
            if hasattr(module, "refresh_codex_oauth_pure"):
                module.refresh_codex_oauth_pure = refresh

        # Without a pool (the bridge's agents before this fix) Hermes's only 401 path refuses the
        # rotated token as an account swap: the turn fails. If this starts passing, Hermes fixed it.
        from agent.client_lifecycle import ClientLifecycleMixin
        pool_less = type("PoolLess", (), {"api_mode": "codex_responses", "provider": "openai-codex", "api_key": A1})()
        assert ClientLifecycleMixin._try_refresh_codex_client_credentials(pool_less) is False

        entry = pool.try_refresh_matching(api_key_hint=A1)  # what Hermes runs on the 401
        assert entry is not None and tag(entry.runtime_api_key) == "A3", entry
        assert posted == ["R2"], posted  # the LIVE refresh token, not the stale one the agent knew
        tokens = json.loads(root_auth.read_text())["providers"]["openai-codex"]["tokens"]
        assert tag(tokens["access_token"]) == "A3" and tokens["refresh_token"] == "R3", tokens
        profile_auth = sync._profile_dir("ceo") / "auth.json"
        forked = profile_auth.exists() and json.loads(profile_auth.read_text()).get("providers", {}).get("openai-codex")
        assert not forked, "the profile forked the root grant"
print("ok: pool refresh adopted the rotated grant and wrote it back to the root store")
