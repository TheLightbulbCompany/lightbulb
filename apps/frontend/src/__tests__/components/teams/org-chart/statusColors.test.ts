import { test, expect } from "vitest";
import {
  STATUS_DOT_CLASS,
  STATUS_DOT_COLOR,
  statusDotClass,
  statusDotColor,
} from "@/components/teams/org-chart/statusColors";

const DEFAULT_COLOR = "var(--mute)";
const DEFAULT_CLASS = "";

test("statusDotClass returns the idle class for 'idle'", () => {
  expect(statusDotClass("idle")).toBe(STATUS_DOT_CLASS.idle);
});

test("statusDotClass marks 'running' with the pulsing animation class", () => {
  const cls = statusDotClass("running");
  expect(cls).toContain("animate-pulse");
});

test("statusDotColor maps 'running' to the editorial forest token", () => {
  expect(statusDotColor("running")).toBe("var(--forest)");
});

test("statusDotColor maps 'error' to the editorial danger token", () => {
  expect(statusDotColor("error")).toBe("var(--danger)");
});

test("statusDotColor maps 'idle' / 'paused' to the mute token", () => {
  expect(statusDotColor("idle")).toBe("var(--mute)");
  expect(statusDotColor("paused")).toBe("var(--mute)");
});

test("statusDotColor maps 'thinking' to the warn token", () => {
  expect(statusDotColor("thinking")).toBe("var(--warn)");
});

test("statusDotColor maps 'covered' to forest-soft and 'stalled' to warn", () => {
  expect(statusDotColor("covered")).toBe("var(--forest-soft)");
  expect(statusDotColor("stalled")).toBe("var(--warn)");
});

test("statusDotClass falls back to the default class for unknown statuses", () => {
  expect(statusDotClass("unknown_status")).toBe(DEFAULT_CLASS);
});

test("statusDotColor falls back to the default color for unknown statuses", () => {
  expect(statusDotColor("unknown_status")).toBe(DEFAULT_COLOR);
});

test("statusDotClass returns the default class for null / undefined", () => {
  expect(statusDotClass(null)).toBe(DEFAULT_CLASS);
  expect(statusDotClass(undefined)).toBe(DEFAULT_CLASS);
});

test("statusDotColor returns the default color for null / undefined", () => {
  expect(statusDotColor(null)).toBe(DEFAULT_COLOR);
  expect(statusDotColor(undefined)).toBe(DEFAULT_COLOR);
});

test("STATUS_DOT_COLOR has all expected status keys", () => {
  for (const key of [
    "active",
    "online",
    "running",
    "thinking",
    "idle",
    "paused",
    "terminated",
    "error",
    "failed",
    "covered",
    "stalled",
  ]) {
    expect(STATUS_DOT_COLOR[key]).toBeDefined();
  }
});
