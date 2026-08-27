import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";
import { describe, expect, test } from "vitest";
import {
  OPENCLAW_CONFIG_GUARD_SUFFIX,
  OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL,
  OPENCLAW_CONFIG_PROTOCOL_MIGRATING,
  OPENCLAW_CONFIG_PROTOCOL_SUFFIX,
  OPENCLAW_CONFIG_LOCK_RECOVERY_PYTHON,
  openclawConfigLockRecoveryCommand,
} from "../lib/openclaw-config-lock-recovery";

function fixturePath(): string {
  const dir = mkdtempSync(join(tmpdir(), "lightbulb-config-lock-"));
  return join(dir, "openclaw.json.lock");
}

function protocolPath(lockPath: string): string {
  return `${lockPath}${OPENCLAW_CONFIG_PROTOCOL_SUFFIX}`;
}

function recover(lockPath: string, timeoutSeconds = 0) {
  return spawnSync(
    "python3",
    ["-c", OPENCLAW_CONFIG_LOCK_RECOVERY_PYTHON, lockPath, String(timeoutSeconds)],
    { encoding: "utf8" },
  );
}

async function holdFcntlLock(path: string) {
  const holder = spawn(
    "python3",
    [
      "-c",
      [
        "import fcntl, os, sys",
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)",
        "fcntl.lockf(fd, fcntl.LOCK_EX)",
        "print('locked', flush=True)",
        "sys.stdin.buffer.read(1)",
        "fcntl.lockf(fd, fcntl.LOCK_UN)",
        "os.close(fd)",
      ].join("\n"),
      path,
    ],
    { stdio: ["pipe", "pipe", "pipe"] },
  );
  await once(holder.stdout, "data");
  return holder;
}

describe("OpenClaw config-lock boot recovery", () => {
  test("protocol version matches the backend task-definition cloner", () => {
    const ecsManager = readFileSync(
      join(__dirname, "../../backend/core/containers/ecs_manager.py"),
      "utf8",
    );
    const backendLock = readFileSync(
      join(__dirname, "../../backend/core/services/efs_file_lock.py"),
      "utf8",
    );
    expect(ecsManager).toContain('_CONFIG_LOCK_PROTOCOL_VERSION = "guard-v1"');
    expect(backendLock).toContain('OPENCLAW_CONFIG_GUARD_SUFFIX = ".lightbulb-guard"');
    expect(backendLock).toContain('OPENCLAW_CONFIG_PROTOCOL_SUFFIX = ".lightbulb-protocol"');
    // Dual-read of the pre-rebrand sentinels must stay in lockstep on both sides.
    expect(backendLock).toContain('OPENCLAW_CONFIG_LEGACY_GUARD_SUFFIX = ".isol8-guard"');
    expect(backendLock).toContain('OPENCLAW_CONFIG_LEGACY_PROTOCOL_SUFFIX = ".isol8-protocol"');
    expect(backendLock).toContain('OPENCLAW_CONFIG_PROTOCOL_MIGRATING = b"migrating-v1\\n"');
    expect(backendLock).toContain('OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL = b"ephemeral-v1\\n"');
  });

  test("per-user replacement stops the old OpenClaw task before boot recovery", () => {
    const ecsManager = readFileSync(
      join(__dirname, "../../backend/core/containers/ecs_manager.py"),
      "utf8",
    );
    expect(ecsManager).toContain('"minimumHealthyPercent": 0');
    expect(ecsManager).toContain('"maximumPercent": 100');
    expect(ecsManager).toContain("deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG");
  });

  test("mixed-version task skips recovery until the backend marks guard-v1", () => {
    const lockPath = fixturePath();
    writeFileSync(lockPath, "");
    const command = openclawConfigLockRecoveryCommand(lockPath, 0);

    const oldBackend = spawnSync("sh", ["-c", command], { encoding: "utf8", env: {} });
    expect(oldBackend.status).toBe(0);
    expect(oldBackend.stdout).toContain("recovery skipped");
    expect(existsSync(lockPath)).toBe(true);

    const updatedBackend = spawnSync("sh", ["-c", command], {
      encoding: "utf8",
      env: { LIGHTBULB_CONFIG_LOCK_PROTOCOL: "guard-v1" },
    });
    expect(updatedBackend.status).toBe(0);
    expect(existsSync(lockPath)).toBe(false);
    expect(readFileSync(protocolPath(lockPath), "utf8")).toBe(OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL);
  });

  test("recovery failure stops the semicolon-joined startup chain", () => {
    const lockPath = fixturePath();
    const reachedPath = `${lockPath}.startup-continued`;
    writeFileSync(lockPath, '{"pid":123}');
    const command = `${openclawConfigLockRecoveryCommand(lockPath, 0)}; printf reached > '${reachedPath}'`;

    const result = spawnSync("sh", ["-c", command], {
      encoding: "utf8",
      env: { LIGHTBULB_CONFIG_LOCK_PROTOCOL: "guard-v1" },
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("not an empty legacy backend sidecar");
    expect(existsSync(reachedPath)).toBe(false);
  });

  test("removes a legacy sidecar and later recovers an abandoned ephemeral sidecar", () => {
    const lockPath = fixturePath();
    writeFileSync(lockPath, "");

    const result = recover(lockPath);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain("recovered stale legacy openclaw.json.lock");
    expect(existsSync(lockPath)).toBe(false);
    expect(existsSync(`${lockPath}${OPENCLAW_CONFIG_GUARD_SUFFIX}`)).toBe(true);
    expect(readFileSync(protocolPath(lockPath), "utf8")).toBe(OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL);

    // After the transition, a backend crash can strand its empty O_EXCL file
    // and a stopped OpenClaw task can strand its JSON owner payload. At boot,
    // the guard excludes every prepared backend writer and stop-first excludes
    // the prior OpenClaw process, so the replacement can recover either.
    writeFileSync(lockPath, '{"pid":123,"createdAt":"2026-08-15T00:00:00Z"}');
    const laterBoot = recover(lockPath);
    expect(laterBoot.status).toBe(0);
    expect(laterBoot.stdout).toContain("recovered abandoned ephemeral openclaw.json.lock");
    expect(existsSync(lockPath)).toBe(false);
    expect(readFileSync(protocolPath(lockPath), "utf8")).toBe(OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL);

    writeFileSync(lockPath, "");
    const backendCrashBoot = recover(lockPath);
    expect(backendCrashBoot.status).toBe(0);
    expect(backendCrashBoot.stdout).toContain("recovered abandoned ephemeral openclaw.json.lock");
    expect(existsSync(lockPath)).toBe(false);
  });

  test("marks a config with no legacy sidecar as ephemeral", () => {
    const lockPath = fixturePath();

    const result = recover(lockPath);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain("no sidecar");
    expect(readFileSync(protocolPath(lockPath), "utf8")).toBe(OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL);
  });

  test("resumes a transition interrupted after publishing migrating-v1", () => {
    const lockPath = fixturePath();
    writeFileSync(lockPath, "");
    writeFileSync(protocolPath(lockPath), OPENCLAW_CONFIG_PROTOCOL_MIGRATING);

    const result = recover(lockPath);

    expect(result.status).toBe(0);
    expect(existsSync(lockPath)).toBe(false);
    expect(readFileSync(protocolPath(lockPath), "utf8")).toBe(OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL);
  });

  test.each(["directory", "symlink"])("fails closed for a %s sidecar", (kind) => {
    const lockPath = fixturePath();
    if (kind === "directory") {
      mkdirSync(lockPath);
    } else {
      const target = `${lockPath}.target`;
      writeFileSync(target, "do-not-touch");
      symlinkSync(target, lockPath);
    }

    const result = recover(lockPath);

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("not a regular non-symlink file");
  });

  test("retains a sidecar held by a legacy backend fcntl writer", async () => {
    const lockPath = fixturePath();
    const holder = await holdFcntlLock(lockPath);
    try {
      const result = recover(lockPath);
      expect(result.status).toBe(1);
      expect(result.stderr).toContain("held by a live backend writer");
    } finally {
      holder.stdin.write("x");
      await once(holder, "close");
    }
    expect(recover(lockPath).status).toBe(0);
  });

  test("fails closed for a non-empty sidecar before the one-time transition", () => {
    const lockPath = fixturePath();
    writeFileSync(lockPath, '{"pid":123}');

    const result = recover(lockPath);

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("not an empty legacy backend sidecar");
    expect(existsSync(lockPath)).toBe(true);
    expect(existsSync(protocolPath(lockPath))).toBe(false);
  });

  test("fails closed for an invalid protocol marker", () => {
    const lockPath = fixturePath();
    writeFileSync(lockPath, "");
    writeFileSync(protocolPath(lockPath), "unknown-v1\n");

    const result = recover(lockPath);

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("protocol marker has an invalid value");
    expect(existsSync(lockPath)).toBe(true);
  });

  test("does not pass the backend recovery guard while a writer holds it", async () => {
    const lockPath = fixturePath();
    writeFileSync(lockPath, "");
    const holder = await holdFcntlLock(`${lockPath}${OPENCLAW_CONFIG_GUARD_SUFFIX}`);
    try {
      const result = recover(lockPath);
      expect(result.status).toBe(1);
      expect(result.stderr).toContain("recovery guard is held by a live backend writer");
    } finally {
      holder.stdin.write("x");
      await once(holder, "close");
    }
  });

  test("retains a post-transition sidecar while a backend writer holds the guard", async () => {
    const lockPath = fixturePath();
    writeFileSync(lockPath, '{"pid":123,"createdAt":"2026-08-15T00:00:00Z"}');
    writeFileSync(protocolPath(lockPath), OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL);
    const holder = await holdFcntlLock(`${lockPath}${OPENCLAW_CONFIG_GUARD_SUFFIX}`);
    try {
      const result = recover(lockPath);
      expect(result.status).toBe(1);
      expect(result.stderr).toContain("recovery guard is held by a live backend writer");
      expect(existsSync(lockPath)).toBe(true);
    } finally {
      holder.stdin.write("x");
      await once(holder, "close");
    }

    expect(recover(lockPath).status).toBe(0);
    expect(existsSync(lockPath)).toBe(false);
  });
});
