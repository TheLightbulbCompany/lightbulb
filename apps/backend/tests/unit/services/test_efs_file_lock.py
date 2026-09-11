import os
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext

import pytest

import core.services.efs_file_lock as efs_file_lock
from core.services.efs_file_lock import (
    OPENCLAW_CONFIG_GUARD_SUFFIX,
    OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL,
    OPENCLAW_CONFIG_PROTOCOL_MIGRATING,
    OPENCLAW_CONFIG_PROTOCOL_SUFFIX,
    SidecarLockIdentityError,
    SidecarLockTimeoutError,
    _openclaw_config_sidecar_under_guard,
    get_file_lock,
    openclaw_config_recovery_guard,
    openclaw_config_sidecar_lock,
)


def _write_protocol(lock_path, value):
    (tmp_path := lock_path.parent / f"{lock_path.name}{OPENCLAW_CONFIG_PROTOCOL_SUFFIX}").write_bytes(value)
    return tmp_path


def _child_can_acquire(path):
    code = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
else:
    sys.exit(0)
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert child.returncode in (0, 1), child.stderr
    return child.returncode == 0


def test_openclaw_sidecar_preserves_legacy_protocol_before_recovery(tmp_path):
    lock_path = tmp_path / "openclaw.json.lock"

    with openclaw_config_sidecar_lock(str(tmp_path), str(lock_path)):
        assert lock_path.is_file()

    assert lock_path.is_file()
    assert lock_path.read_bytes() == b""
    assert (tmp_path / f"openclaw.json.lock{OPENCLAW_CONFIG_GUARD_SUFFIX}").is_file()


@pytest.mark.parametrize(
    "protocol",
    [OPENCLAW_CONFIG_PROTOCOL_MIGRATING, OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL],
)
def test_openclaw_sidecar_is_exclusive_empty_and_ephemeral_after_recovery(tmp_path, protocol):
    lock_path = tmp_path / "openclaw.json.lock"
    _write_protocol(lock_path, protocol)

    with openclaw_config_sidecar_lock(str(tmp_path), str(lock_path)):
        assert lock_path.exists()
        assert lock_path.read_bytes() == b""
        with pytest.raises(FileExistsError):
            os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)

    assert not lock_path.exists()
    assert (tmp_path / f"openclaw.json.lock{OPENCLAW_CONFIG_GUARD_SUFFIX}").is_file()


def test_openclaw_sidecar_waits_for_openclaw_owner_without_reclaiming(tmp_path):
    lock_path = tmp_path / "openclaw.json.lock"
    _write_protocol(lock_path, OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL)
    lock_path.write_text('{"pid":123,"createdAt":"2026-08-15T00:00:00Z"}')

    with pytest.raises(SidecarLockTimeoutError, match="sidecar"):
        with openclaw_config_sidecar_lock(
            str(tmp_path),
            str(lock_path),
            timeout_s=0,
        ):
            raise AssertionError("must not acquire an existing OpenClaw sidecar")

    assert lock_path.read_text() == '{"pid":123,"createdAt":"2026-08-15T00:00:00Z"}'


def test_migrating_protocol_does_not_reopen_legacy_inode(tmp_path):
    lock_path = tmp_path / "openclaw.json.lock"
    lock_path.write_bytes(b"")
    _write_protocol(lock_path, OPENCLAW_CONFIG_PROTOCOL_MIGRATING)

    with pytest.raises(SidecarLockTimeoutError, match="sidecar"):
        with openclaw_config_sidecar_lock(
            str(tmp_path),
            str(lock_path),
            timeout_s=0,
        ):
            raise AssertionError("must not fall back to the legacy inode during recovery")

    assert lock_path.is_file()


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_openclaw_sidecar_fails_closed_for_non_regular_path(tmp_path, kind):
    lock_path = tmp_path / "openclaw.json.lock"
    _write_protocol(lock_path, OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL)
    if kind == "directory":
        lock_path.mkdir()
    else:
        target = tmp_path / "target"
        target.write_text("do-not-touch")
        lock_path.symlink_to(target)

    with pytest.raises(SidecarLockIdentityError, match="not a regular file"):
        with openclaw_config_sidecar_lock(
            str(tmp_path),
            str(lock_path),
            timeout_s=0,
        ):
            raise AssertionError("must not acquire an unexpected filesystem object")


def test_openclaw_sidecar_does_not_unlink_replacement_inode(tmp_path):
    lock_path = tmp_path / "openclaw.json.lock"
    _write_protocol(lock_path, OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL)

    with pytest.raises(SidecarLockIdentityError, match="changed identity"):
        with openclaw_config_sidecar_lock(str(tmp_path), str(lock_path)):
            lock_path.unlink()
            lock_path.write_text("replacement")

    assert lock_path.read_text() == "replacement"


def test_openclaw_sidecar_fails_closed_for_invalid_protocol_marker(tmp_path):
    lock_path = tmp_path / "openclaw.json.lock"
    lock_path.write_text("legacy")
    _write_protocol(lock_path, b"unknown-v1\n")

    with pytest.raises(SidecarLockIdentityError, match="invalid OpenClaw config protocol marker"):
        with openclaw_config_sidecar_lock(str(tmp_path), str(lock_path)):
            raise AssertionError("must not select a protocol from an invalid marker")

    assert lock_path.read_text() == "legacy"


def test_recovery_guard_fails_if_path_no_longer_names_locked_inode(tmp_path):
    lock_path = tmp_path / "openclaw.json.lock"
    guard_path = tmp_path / f"openclaw.json.lock{OPENCLAW_CONFIG_GUARD_SUFFIX}"

    with pytest.raises(SidecarLockIdentityError, match="guard changed identity"):
        with openclaw_config_recovery_guard(str(tmp_path), str(lock_path)):
            guard_path.unlink()
            guard_path.write_text("replacement")

    assert guard_path.read_text() == "replacement"


@pytest.mark.parametrize(
    ("protocol", "sidecar_persists"),
    [(None, True), (OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL, False)],
)
@pytest.mark.parametrize("body_error", [False, True])
def test_sidecar_under_guard_composes_with_guard_and_propagates_body_error(
    tmp_path, protocol, sidecar_persists, body_error
):
    lock_path = tmp_path / "openclaw.json.lock"
    guard_path = tmp_path / f"openclaw.json.lock{OPENCLAW_CONFIG_GUARD_SUFFIX}"
    if protocol is not None:
        _write_protocol(lock_path, protocol)

    with get_file_lock(str(lock_path)):
        with openclaw_config_recovery_guard(str(tmp_path), str(lock_path)):
            assert not _child_can_acquire(guard_path)
            with pytest.raises(RuntimeError, match="body failed") if body_error else nullcontext():
                with _openclaw_config_sidecar_under_guard(
                    str(tmp_path),
                    str(lock_path),
                    deadline=time.monotonic() + 1,
                    poll_interval_s=0.01,
                ):
                    assert not _child_can_acquire(guard_path)
                    assert lock_path.is_file()
                    if body_error:
                        raise RuntimeError("body failed")
            assert not _child_can_acquire(guard_path)

        assert _child_can_acquire(guard_path)

    assert lock_path.is_file() is sidecar_persists


def test_public_sidecar_wrapper_forwards_pre_guard_deadline_and_poll_interval(tmp_path, monkeypatch):
    lock_path = tmp_path / "openclaw.json.lock"
    calls = []

    @contextmanager
    def recording_sidecar(lock_dir, sidecar_path, *, deadline, poll_interval_s):
        calls.append((lock_dir, sidecar_path, deadline, poll_interval_s))
        yield

    @contextmanager
    def no_guard(*args, **kwargs):
        yield

    monkeypatch.setattr(efs_file_lock.time, "monotonic", lambda: 42)
    monkeypatch.setattr(efs_file_lock, "openclaw_config_recovery_guard", no_guard)
    monkeypatch.setattr(
        efs_file_lock,
        "_openclaw_config_sidecar_under_guard",
        recording_sidecar,
    )

    with openclaw_config_sidecar_lock(str(tmp_path), str(lock_path), timeout_s=7, poll_interval_s=0.01):
        pass

    assert calls == [(str(tmp_path), str(lock_path), 49, 0.01)]
