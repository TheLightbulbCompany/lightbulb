"""Tests for the EFS pre-staging of OpenClaw's container-global ``oauth.json``.

OpenClaw 6.6 reads per-agent auth from a WAL SQLite store and IGNORES the
legacy per-agent ``auth-profiles.json``. The supported external seed is the
container-global ``<stateDir>/credentials/oauth.json`` (EFS
``<owner>/credentials/oauth.json``), which the in-container process imports
into each agent's SQLite store on first load (building profile
``openai:default``) under a cross-agent ``(provider, profileId)`` refresh lock
that serializes token rotation. So we write ONE flat file, not a per-agent
fan-out (the fan-out broke under refresh-token rotation: refresh_token_reused).
See project_openclaw_oauth_auth_model.
"""

import json
import sqlite3

import pytest

from core.containers.workspace import (
    clear_default_agent_auth_profile_row,
    delete_auth_profile_store,
    pre_stage_auth_profile_store,
)

# The oauth.json top-level key is the PROVIDER name; 6.6 builds profile
# `<provider>:default` from it. Provider MUST be "openai" (the retired
# "openai-codex" is silently dropped — there is no runtime alias).
_PROVIDER_KEY = "openai"


@pytest.fixture
def fake_efs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("EFS_MOUNT_PATH", str(tmp_path))
    return tmp_path


def _oauth_path(efs_root, user_id):
    return efs_root / user_id / "credentials" / "oauth.json"


@pytest.mark.asyncio
async def test_writes_flat_oauth_json(fake_efs_root):
    tokens = {
        "access_token": "eyJ.access",
        "refresh_token": "rt_opaque",
        "expires_at": 1730000000000,
        "email": "user@example.com",
    }
    await pre_stage_auth_profile_store(user_id="u_1", oauth_tokens=tokens)

    written = json.loads(_oauth_path(fake_efs_root, "u_1").read_text())
    # Flat Record<provider, OAuthCredentials> — NOT the {version, profiles}
    # AuthProfileStore wrapper, and NOT keyed by profile id.
    assert set(written.keys()) == {_PROVIDER_KEY}
    assert "version" not in written and "profiles" not in written
    creds = written[_PROVIDER_KEY]
    assert creds["access"] == "eyJ.access"
    assert creds["refresh"] == "rt_opaque"
    assert creds["expires"] == 1730000000000
    assert creds["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_single_file_no_per_agent_fanout(fake_efs_root):
    """Exactly ONE file at credentials/oauth.json — never per-agent."""
    # Pre-create agent dirs to prove we do NOT write into any of them.
    for aid in ("main", "friday-uuid", "atlas-uuid"):
        (fake_efs_root / "org_1" / "agents" / aid / "agent").mkdir(parents=True)

    await pre_stage_auth_profile_store(
        user_id="org_1",
        oauth_tokens={"access_token": "a", "refresh_token": "b"},
    )

    assert _oauth_path(fake_efs_root, "org_1").exists()
    leaked = list((fake_efs_root / "org_1" / "agents").rglob("auth-profiles.json"))
    assert leaked == [], f"unexpected per-agent fan-out: {leaked}"


@pytest.mark.asyncio
async def test_field_mapping_from_ddb_keys(fake_efs_root):
    """DDB access_token/refresh_token/expires_at/account_id ->
    OAuthCredentials access/refresh/expires/accountId."""
    await pre_stage_auth_profile_store(
        user_id="u_map",
        oauth_tokens={
            "access_token": "A",
            "refresh_token": "R",
            "expires_at": 42,
            "account_id": "acct",
        },
    )
    creds = json.loads(_oauth_path(fake_efs_root, "u_map").read_text())[_PROVIDER_KEY]
    assert creds == {"access": "A", "refresh": "R", "expires": 42, "accountId": "acct"}


@pytest.mark.asyncio
async def test_omits_optional_fields(fake_efs_root):
    await pre_stage_auth_profile_store(
        user_id="u_min",
        oauth_tokens={"access_token": "a", "refresh_token": "b"},
    )
    creds = json.loads(_oauth_path(fake_efs_root, "u_min").read_text())[_PROVIDER_KEY]
    assert creds == {"access": "a", "refresh": "b"}
    assert "expires" not in creds and "email" not in creds and "accountId" not in creds


@pytest.mark.asyncio
async def test_overwrites_on_reauth(fake_efs_root):
    """Re-OAuth: rewrite the file with new tokens, no merge."""
    await pre_stage_auth_profile_store(user_id="u_1", oauth_tokens={"access_token": "old", "refresh_token": "old_rt"})
    await pre_stage_auth_profile_store(user_id="u_1", oauth_tokens={"access_token": "new", "refresh_token": "new_rt"})
    creds = json.loads(_oauth_path(fake_efs_root, "u_1").read_text())[_PROVIDER_KEY]
    assert creds["access"] == "new"
    assert creds["refresh"] == "new_rt"


@pytest.mark.asyncio
async def test_creates_credentials_dir(fake_efs_root):
    """The credentials/ subdir doesn't exist yet — helper must mkdir -p."""
    await pre_stage_auth_profile_store(user_id="brand_new", oauth_tokens={"access_token": "x", "refresh_token": "y"})
    assert _oauth_path(fake_efs_root, "brand_new").exists()


@pytest.mark.asyncio
async def test_chown_failure_is_non_fatal(fake_efs_root, monkeypatch):
    def raising_chown(*args, **kwargs):
        raise PermissionError("can't chown in test env")

    monkeypatch.setattr("os.chown", raising_chown)
    await pre_stage_auth_profile_store(user_id="u_perm", oauth_tokens={"access_token": "x", "refresh_token": "y"})
    creds = json.loads(_oauth_path(fake_efs_root, "u_perm").read_text())[_PROVIDER_KEY]
    assert creds["access"] == "x"


@pytest.mark.asyncio
async def test_rejects_path_traversal_user_id(fake_efs_root):
    with pytest.raises(ValueError, match="invalid characters"):
        await pre_stage_auth_profile_store(
            user_id="../etc/passwd",
            oauth_tokens={"access_token": "x", "refresh_token": "y"},
        )
    assert not (fake_efs_root / "etc" / "passwd").exists()


@pytest.mark.asyncio
async def test_rejects_missing_required_tokens(fake_efs_root):
    with pytest.raises(ValueError, match="missing required keys"):
        await pre_stage_auth_profile_store(
            user_id="u_x",
            oauth_tokens={"access_token": "x"},  # no refresh_token
        )


@pytest.mark.asyncio
async def test_delete_removes_oauth_json(fake_efs_root):
    await pre_stage_auth_profile_store(user_id="u_del", oauth_tokens={"access_token": "x", "refresh_token": "y"})
    path = _oauth_path(fake_efs_root, "u_del")
    assert path.exists()

    await delete_auth_profile_store(user_id="u_del")
    assert not path.exists()


@pytest.mark.asyncio
async def test_delete_sweeps_legacy_per_agent_files(fake_efs_root):
    """Disconnect must also sweep any legacy fanned-out auth-profiles.json
    (pre-6.6 model), or a revoked agent keeps a usable refresh token on EFS."""
    user = "u_legacy"
    for aid in ("main", "friday-uuid"):
        d = fake_efs_root / user / "agents" / aid / "agent"
        d.mkdir(parents=True)
        (d / "auth-profiles.json").write_text("{}")

    await pre_stage_auth_profile_store(user_id=user, oauth_tokens={"access_token": "x", "refresh_token": "y"})
    await delete_auth_profile_store(user_id=user)

    assert not _oauth_path(fake_efs_root, user).exists()
    leaked = list((fake_efs_root / user / "agents").rglob("auth-profiles.json"))
    assert leaked == [], f"legacy auth-profiles.json not swept: {leaked}"


@pytest.mark.asyncio
async def test_delete_no_op_when_absent(fake_efs_root):
    """Disconnect on a never-staged user must not raise."""
    await delete_auth_profile_store(user_id="u_never")  # should not raise


# ---------------------------------------------------------------------------
# clear_default_agent_auth_profile_row — make the default agent's persisted
# auth store ABSENT so OpenClaw 6.6's boot import (mergeOAuthFileIntoStore,
# which only fires on a missing store row) re-imports a freshly staged
# oauth.json. authLogout/disconnect EMPTIES the row to {"profiles":{}}, which
# still counts as "store exists" and shadows the import — so re-auth on a
# running container needs the row deleted, not just emptied. See
# project_openclaw_oauth_auth_model.
# ---------------------------------------------------------------------------

# Mirrors the live OpenClaw 6.6 per-agent auth store schema (verified on a
# prod container): table auth_profile_store(store_key, store_json, updated_at),
# the persisted store keyed store_key="primary".
_EMPTY_SHADOW_STORE = '{"version":1,"profiles":{}}'


def _agent_sqlite_path(efs_root, user_id, agent):
    return efs_root / user_id / "agents" / agent / "agent" / "openclaw-agent.sqlite"


def _make_agent_sqlite(efs_root, user_id, agent, store_json=_EMPTY_SHADOW_STORE):
    p = _agent_sqlite_path(efs_root, user_id, agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE auth_profile_store (store_key TEXT, store_json TEXT, updated_at INTEGER)")
    con.execute(
        "INSERT INTO auth_profile_store (store_key, store_json, updated_at) VALUES ('primary', ?, 1)",
        (store_json,),
    )
    con.commit()
    con.close()
    return p


def _store_rows(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT store_key FROM auth_profile_store").fetchall()
    finally:
        con.close()


@pytest.mark.asyncio
async def test_clear_default_agent_deletes_primary_row(fake_efs_root):
    """Deletes the 'primary' row from main's store so the row is ABSENT
    (loadPersistedAuthProfileStore -> null -> the oauth.json import re-fires)."""
    path = _make_agent_sqlite(fake_efs_root, "org_1", "main")
    assert _store_rows(path) == [("primary",)]

    await clear_default_agent_auth_profile_row(user_id="org_1")

    assert _store_rows(path) == [], "primary row must be gone, not just emptied"


@pytest.mark.asyncio
async def test_clear_default_agent_only_touches_main(fake_efs_root):
    """Single-source model: only the default agent (main) is cleared; non-default
    agents read through main, so their rows are left intact (clearing them would
    make each re-import its own copy -> refresh_token_reused fan-out)."""
    main_path = _make_agent_sqlite(fake_efs_root, "org_1", "main")
    other_path = _make_agent_sqlite(fake_efs_root, "org_1", "friday-uuid")

    await clear_default_agent_auth_profile_row(user_id="org_1")

    assert _store_rows(main_path) == []
    assert _store_rows(other_path) == [("primary",)], "non-default agent untouched"


@pytest.mark.asyncio
async def test_clear_default_agent_noop_when_sqlite_missing(fake_efs_root):
    """No store yet (fresh/un-booted container) must not raise."""
    await clear_default_agent_auth_profile_row(user_id="u_never")  # should not raise


@pytest.mark.asyncio
async def test_clear_default_agent_noop_when_table_missing(fake_efs_root):
    """A sqlite without the auth_profile_store table must not raise."""
    p = _agent_sqlite_path(fake_efs_root, "org_1", "main")
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE other (x TEXT)")
    con.commit()
    con.close()

    await clear_default_agent_auth_profile_row(user_id="org_1")  # should not raise


@pytest.mark.asyncio
async def test_clear_default_agent_locked_db_is_loud_not_silent(fake_efs_root, monkeypatch):
    """A genuine write failure (SQLITE_BUSY — the live OpenClaw writer held the
    lock past the busy-timeout) must NOT be masked as a benign no-op: the stale
    row survives and would silently block the oauth.json re-import while the
    switch reports success. Emit a failure metric + return False (no raise)."""
    _make_agent_sqlite(fake_efs_root, "org_1", "main")  # file must exist to reach connect()

    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    metrics: list[str] = []
    monkeypatch.setattr("core.containers.workspace.sqlite3.connect", _locked)
    monkeypatch.setattr("core.containers.workspace.put_metric", lambda name, **kw: metrics.append(name))

    result = await clear_default_agent_auth_profile_row(user_id="org_1")  # must not raise
    assert result is False
    assert any("clear" in m and "fail" in m for m in metrics), f"expected a failure metric, got {metrics}"


@pytest.mark.asyncio
async def test_clear_default_agent_missing_table_emits_no_failure_metric(fake_efs_root, monkeypatch):
    """Missing auth_profile_store table (un-booted container) is a benign no-op —
    return False WITHOUT a failure metric, so it's distinguishable from a real
    write failure in monitoring."""
    p = _agent_sqlite_path(fake_efs_root, "org_1", "main")
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE other (x TEXT)")
    con.commit()
    con.close()

    metrics: list[str] = []
    monkeypatch.setattr("core.containers.workspace.put_metric", lambda name, **kw: metrics.append(name))

    result = await clear_default_agent_auth_profile_row(user_id="org_1")
    assert result is False
    assert metrics == [], f"missing-table must not emit a failure metric, got {metrics}"


# ---------------------------------------------------------------------------
# O6 local-SQLite layout: the per-agent store lives on container-local disk.
# With LITESTREAM_BUCKET set and no store on EFS, the clear writes a boot
# sentinel and reports restart_required; an EFS store (old-layout container in
# the same env during the cutover window) still wins with the hot DELETE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_writes_o6_sentinel_when_store_is_container_local(fake_efs_root, monkeypatch):
    from core.config import settings as core_settings
    from core.containers.workspace import O6_CLEAR_AUTH_SENTINEL_FILENAME

    monkeypatch.setattr(core_settings, "LITESTREAM_BUCKET", "isol8-dev-openclaw-replicas")

    result = await clear_default_agent_auth_profile_row(user_id="org_1")

    assert result == "restart_required"
    sentinel = fake_efs_root / "org_1" / O6_CLEAR_AUTH_SENTINEL_FILENAME
    data = json.loads(sentinel.read_text())
    assert data["agent_ids"] == ["main"]


@pytest.mark.asyncio
async def test_clear_does_the_efs_delete_when_the_store_is_visible(fake_efs_root, monkeypatch):
    """An EFS store still gets the hot in-place DELETE — but see
    test_clear_writes_sentinel_even_when_efs_store_still_visible: on an O6 env
    the sentinel is ALSO written, because during the cutover boot the EFS copy
    may already be the stale one."""
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "LITESTREAM_BUCKET", "isol8-dev-openclaw-replicas")
    path = _make_agent_sqlite(fake_efs_root, "org_1", "main")

    result = await clear_default_agent_auth_profile_row(user_id="org_1")

    assert result is True
    assert _store_rows(path) == []


@pytest.mark.asyncio
async def test_clear_stays_benign_noop_without_o6_bucket(fake_efs_root):
    """No LITESTREAM_BUCKET (prod/old layout): a missing store returns False
    and writes NOTHING — first-connect-during-onboarding stays a pure no-op."""
    result = await clear_default_agent_auth_profile_row(user_id="u_never")

    assert result is False
    assert not (fake_efs_root / "u_never").exists()


@pytest.mark.asyncio
async def test_clear_writes_sentinel_even_when_efs_store_still_visible(fake_efs_root, monkeypatch):
    """During the one cutover boot the container has already copied the DBs to
    its local mounts but has not yet archived the EFS originals. A hot DELETE
    landing in that window edits a copy nothing reads again, so the stale row
    survives locally and the reconnect silently fails. Write both."""
    from core.config import settings as core_settings
    from core.containers.workspace import O6_CLEAR_AUTH_SENTINEL_FILENAME

    monkeypatch.setattr(core_settings, "LITESTREAM_BUCKET", "isol8-dev-openclaw-replicas")
    path = _make_agent_sqlite(fake_efs_root, "org_1", "main")

    result = await clear_default_agent_auth_profile_row(user_id="org_1")

    assert result is True  # the in-place delete still happened
    assert _store_rows(path) == []
    # ...and the sentinel is there so the container-local copy gets cleared too
    assert (fake_efs_root / "org_1" / O6_CLEAR_AUTH_SENTINEL_FILENAME).is_file()


# ---------------------------------------------------------------------------
# O6 archive sweep. On an owner's first boot under the local-SQLite layout the
# boot script moves the pre-cutover EFS `state/` + `agents/` wholesale into
# `pre-o6-archive/` — auth_profile_store rows and all. The live sweeps above
# walk `agents/*`, which post-cutover is the recreated-empty mount destination,
# so without an explicit archive pass a disconnected owner keeps a usable
# refresh token on EFS indefinitely.
# ---------------------------------------------------------------------------


def _archive_agent_dir(root, user: str, agent_id: str):
    d = root / user / "pre-o6-archive" / "agents" / agent_id / "agent"
    d.mkdir(parents=True)
    return d


def _seed_archived_store(db_path, *, store_key: str = "primary") -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("CREATE TABLE auth_profile_store(store_key TEXT, store_json TEXT)")
        con.execute(
            "INSERT INTO auth_profile_store VALUES(?, ?)",
            (store_key, json.dumps({"profiles": {"openai:default": {"refresh": "still-usable"}}})),
        )
        con.commit()
    finally:
        con.close()


@pytest.mark.asyncio
async def test_delete_purges_archived_auth_rows(fake_efs_root):
    """A disconnected owner must not keep a live refresh token in the archive."""
    user = "u_archive"
    d = _archive_agent_dir(fake_efs_root, user, "main")
    db = d / "openclaw-agent.sqlite"
    _seed_archived_store(db)

    await delete_auth_profile_store(user_id=user)

    assert _store_rows(db) == [], "archived auth_profile_store row survived disconnect"


@pytest.mark.asyncio
async def test_delete_purges_every_archived_agent_not_just_main(fake_efs_root):
    """The pre-6.6 fan-out means an old archive can hold several independently
    usable tokens, so the sweep is not scoped to the default agent."""
    user = "u_archive_many"
    dbs = []
    for aid in ("main", "friday-uuid", "atlas-uuid"):
        db = _archive_agent_dir(fake_efs_root, user, aid) / "openclaw-agent.sqlite"
        _seed_archived_store(db, store_key=aid)
        dbs.append(db)

    await delete_auth_profile_store(user_id=user)

    for db in dbs:
        assert _store_rows(db) == [], f"archived row survived for {db.parent.parent.name}"


@pytest.mark.asyncio
async def test_delete_sweeps_legacy_archived_auth_profiles_json(fake_efs_root):
    """The legacy per-agent JSON gets archived too, and is just as usable."""
    user = "u_archive_legacy"
    legacy = _archive_agent_dir(fake_efs_root, user, "main") / "auth-profiles.json"
    legacy.write_text("{}")

    await delete_auth_profile_store(user_id=user)

    assert not legacy.exists(), "archived legacy auth-profiles.json not swept"


@pytest.mark.asyncio
async def test_delete_tolerates_archive_without_auth_table(fake_efs_root):
    """An archived DB that never persisted auth is benign, not an error."""
    user = "u_archive_no_table"
    db = _archive_agent_dir(fake_efs_root, user, "main") / "openclaw-agent.sqlite"
    con = sqlite3.connect(str(db))
    try:
        con.execute("CREATE TABLE unrelated(x)")
        con.commit()
    finally:
        con.close()

    await delete_auth_profile_store(user_id=user)  # must not raise


@pytest.mark.asyncio
async def test_delete_no_op_when_no_archive(fake_efs_root):
    """Owners provisioned natively under O6 have no archive at all."""
    await delete_auth_profile_store(user_id="u_no_archive")  # must not raise
