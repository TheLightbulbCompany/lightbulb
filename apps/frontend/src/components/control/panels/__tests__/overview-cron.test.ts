import { describe, it, expect } from "vitest";
import { countCronFailed, countCronOverdue } from "../overview-cron";
import type { CronJob } from "../cron/types";

const NOW = 1_700_000_000_000;

function job(overrides: Partial<CronJob>): CronJob {
  return {
    id: "j1",
    name: "Job",
    enabled: true,
    createdAtMs: 0,
    updatedAtMs: 0,
    schedule: { kind: "every", everyMs: 60_000 },
    sessionTarget: "main",
    wakeMode: "next-heartbeat",
    payload: { kind: "agentTurn", message: "hi" },
    state: {},
    ...overrides,
  };
}

describe("countCronFailed", () => {
  it("counts an enabled job with lastRunStatus=error and >=1 consecutive errors", () => {
    const jobs = [job({ state: { lastRunStatus: "error", consecutiveErrors: 2 } })];
    expect(countCronFailed(jobs)).toBe(1);
  });

  it("honors the lastStatus back-compat alias", () => {
    const jobs = [job({ state: { lastStatus: "error", consecutiveErrors: 1 } })];
    expect(countCronFailed(jobs)).toBe(1);
  });

  it("does not count disabled jobs even when erroring", () => {
    const jobs = [job({ enabled: false, state: { lastRunStatus: "error", consecutiveErrors: 5 } })];
    expect(countCronFailed(jobs)).toBe(0);
  });

  it("does not count an error with zero consecutive errors", () => {
    const jobs = [job({ state: { lastRunStatus: "error", consecutiveErrors: 0 } })];
    expect(countCronFailed(jobs)).toBe(0);
  });

  it("does not count an ok run", () => {
    const jobs = [job({ state: { lastRunStatus: "ok", consecutiveErrors: 0 } })];
    expect(countCronFailed(jobs)).toBe(0);
  });
});

describe("countCronOverdue", () => {
  it("counts an enabled job whose nextRunAtMs is in the past", () => {
    const jobs = [job({ state: { nextRunAtMs: NOW - 1000 } })];
    expect(countCronOverdue(jobs, NOW)).toBe(1);
  });

  it("does not count a future nextRunAtMs", () => {
    const jobs = [job({ state: { nextRunAtMs: NOW + 1000 } })];
    expect(countCronOverdue(jobs, NOW)).toBe(0);
  });

  it("does not count disabled jobs", () => {
    const jobs = [job({ enabled: false, state: { nextRunAtMs: NOW - 1000 } })];
    expect(countCronOverdue(jobs, NOW)).toBe(0);
  });

  it("does not count a job with no nextRunAtMs", () => {
    const jobs = [job({ state: {} })];
    expect(countCronOverdue(jobs, NOW)).toBe(0);
  });

  it("does not count a job that has fired and is still running", () => {
    // nextRunAtMs is in the past because the job already fired, but runningAtMs
    // means the run is still in progress — it isn't overdue, it's executing.
    const jobs = [job({ state: { nextRunAtMs: NOW - 1000, runningAtMs: NOW - 500 } })];
    expect(countCronOverdue(jobs, NOW)).toBe(0);
  });
});
