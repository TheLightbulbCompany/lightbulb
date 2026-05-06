import { test, expect } from "vitest";
import {
  STATUS_DOT_CLASS,
  statusDotClass,
} from "@/components/teams/org-chart/statusColors";

const DEFAULT = "bg-zinc-400 dark:bg-zinc-500";

test("statusDotClass returns the idle class for 'idle'", () => {
  expect(statusDotClass("idle")).toBe(STATUS_DOT_CLASS.idle);
});

test("statusDotClass marks 'running' with the pulsing emerald class", () => {
  const cls = statusDotClass("running");
  expect(cls).toContain("emerald-500");
  expect(cls).toContain("animate-pulse");
});

test("statusDotClass marks 'error' with red-500", () => {
  expect(statusDotClass("error")).toContain("red-500");
});

test("statusDotClass falls back to the default class for unknown statuses", () => {
  expect(statusDotClass("unknown_status")).toBe(DEFAULT);
});

test("statusDotClass returns the default class for null", () => {
  expect(statusDotClass(null)).toBe(DEFAULT);
});

test("statusDotClass returns the default class for undefined", () => {
  expect(statusDotClass(undefined)).toBe(DEFAULT);
});
