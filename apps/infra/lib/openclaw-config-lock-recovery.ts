export const OPENCLAW_CONFIG_LOCK_PATH = "/home/node/.openclaw/openclaw.json.lock";
export const OPENCLAW_CONFIG_GUARD_SUFFIX = ".isol8-guard";
export const OPENCLAW_CONFIG_PROTOCOL_SUFFIX = ".isol8-protocol";
export const OPENCLAW_CONFIG_PROTOCOL_MIGRATING = "migrating-v1\n";
export const OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL = "ephemeral-v1\n";
export const OPENCLAW_CONFIG_LOCK_RECOVERY_TIMEOUT_S = 30;
export const OPENCLAW_CONFIG_LOCK_PROTOCOL_VERSION = "guard-v1";

// Python is baked into every extended OpenClaw image. Keep this helper inline
// in the task definition so recovery does not depend on promoting a new image
// before the base task definition can be used.
export const OPENCLAW_CONFIG_LOCK_RECOVERY_PYTHON = String.raw`
import fcntl
import os
import stat
import sys
import time

lock_path = sys.argv[1]
timeout_s = float(sys.argv[2])
guard_path = lock_path + ".isol8-guard"
protocol_path = lock_path + ".isol8-protocol"
migrating_protocol = b"migrating-v1\n"
ephemeral_protocol = b"ephemeral-v1\n"


def fail(message):
    print("[boot] FATAL: " + message, file=sys.stderr)
    raise SystemExit(1)


def open_regular(path, flags, mode=0o600):
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        fail("cannot open config lock path %s: %s" % (path, exc))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        fail("config lock path is not a regular file: " + path)
    return fd


def path_names_fd(path, fd):
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return False
    opened = os.fstat(fd)
    return (
        stat.S_ISREG(current.st_mode)
        and not stat.S_ISLNK(current.st_mode)
        and current.st_dev == opened.st_dev
        and current.st_ino == opened.st_ino
    )


def acquire_until(fd, deadline, timeout_message):
    while True:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fail(timeout_message)
            time.sleep(0.05)


def require_guard_identity():
    if not path_names_fd(guard_path, guard_fd):
        fail("config-lock recovery guard changed identity")


def read_protocol():
    try:
        observed = os.lstat(protocol_path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        fail("config lock protocol marker is not a regular non-symlink file")
    fd = open_regular(protocol_path, os.O_RDONLY)
    try:
        if not path_names_fd(protocol_path, fd):
            fail("config lock protocol marker changed identity while opening")
        value = os.read(fd, 64)
    finally:
        os.close(fd)
    if value not in (migrating_protocol, ephemeral_protocol):
        fail("config lock protocol marker has an invalid value")
    return value


def write_protocol(value):
    temp_path = protocol_path + ".tmp.%d.%d" % (os.getpid(), time.time_ns())
    fd = open_regular(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(fd, value[offset:])
        os.fsync(fd)
        try:
            os.chown(temp_path, 1000, 1000)
        except OSError:
            pass
    finally:
        os.close(fd)
    try:
        os.replace(temp_path, protocol_path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


os.makedirs(os.path.dirname(lock_path), exist_ok=True)
guard_fd = open_regular(guard_path, os.O_RDWR | os.O_CREAT)
lock_fd = None
try:
    try:
        os.chown(guard_path, 1000, 1000)
    except OSError:
        pass
    deadline = time.monotonic() + max(0.0, timeout_s)
    acquire_until(guard_fd, deadline, "config-lock recovery guard is held by a live backend writer")
    require_guard_identity()

    protocol = read_protocol()

    try:
        observed = os.lstat(lock_path)
    except FileNotFoundError:
        observed = None
    if observed is not None:
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            fail("openclaw.json.lock is not a regular non-symlink file")

        lock_fd = open_regular(lock_path, os.O_RDWR)
        if not path_names_fd(lock_path, lock_fd):
            fail("openclaw.json.lock changed identity while opening")

        acquire_until(lock_fd, deadline, "openclaw.json.lock is held by a live backend writer")
        if not path_names_fd(lock_path, lock_fd):
            fail("openclaw.json.lock changed identity before recovery")
        # Before the transition, only the backend's empty persistent fcntl
        # sidecar is eligible. Afterwards this pathname follows OpenClaw's
        # O_EXCL protocol and may contain its JSON owner metadata. The old
        # OpenClaw task is already stopped, while the guard excludes every
        # prepared backend writer, so either post-transition payload is stale.
        if protocol != ephemeral_protocol and os.read(lock_fd, 1):
            fail("openclaw.json.lock is not an empty legacy backend sidecar")

    if protocol is None:
        # Publishing migrating-v1 first makes an interrupted transition fail
        # closed: every prepared backend switches to O_EXCL and will not open
        # or recreate the legacy inode while the next boot resumes recovery.
        require_guard_identity()
        write_protocol(migrating_protocol)
        require_guard_identity()

    if lock_fd is not None:
        require_guard_identity()
        if not path_names_fd(lock_path, lock_fd):
            fail("openclaw.json.lock changed identity before unlink")
        os.unlink(lock_path)
        if protocol == ephemeral_protocol:
            print("[boot] recovered abandoned ephemeral openclaw.json.lock")
        else:
            print("[boot] recovered stale legacy openclaw.json.lock")
    else:
        print("[boot] config lock recovery: no sidecar")

    if protocol != ephemeral_protocol:
        require_guard_identity()
        write_protocol(ephemeral_protocol)
        require_guard_identity()
    print("[boot] config lock protocol transition complete")
finally:
    if lock_fd is not None:
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    try:
        fcntl.lockf(guard_fd, fcntl.LOCK_UN)
    finally:
        os.close(guard_fd)
`;

function shellQuote(value: string): string {
  return `'${value.split("'").join(`'"'"'`)}'`;
}

export function openclawConfigLockRecoveryCommand(
  lockPath = OPENCLAW_CONFIG_LOCK_PATH,
  timeoutSeconds = OPENCLAW_CONFIG_LOCK_RECOVERY_TIMEOUT_S,
): string {
  const recovery = [
    "python3",
    "-c",
    shellQuote(OPENCLAW_CONFIG_LOCK_RECOVERY_PYTHON),
    shellQuote(lockPath),
    String(timeoutSeconds),
  ].join(" ");
  return [
    `if [ "\${ISOL8_CONFIG_LOCK_PROTOCOL:-}" = "${OPENCLAW_CONFIG_LOCK_PROTOCOL_VERSION}" ]; then`,
    `${recovery} || exit 1;`,
    "else echo '[boot] config lock recovery skipped: backend protocol is not guard-v1'; fi",
  ].join(" ");
}
