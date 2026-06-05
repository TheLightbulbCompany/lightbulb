// Pure cron health counting for the Overview cron card. The failed predicate
// is the same one JobCard renders per job; centralized here so the card and
// the badge can't drift, and so the overdue clock comparison is testable via
// an injected nowMs.

import type { CronJob } from "./cron/types";

// An enabled job whose most recent run errored and that has at least one
// consecutive error is "failing". Mirrors JobCard's showErrorBadge.
export function countCronFailed(jobs: CronJob[]): number {
  return jobs.filter((job) => {
    const lastRunStatus = job.state?.lastRunStatus ?? job.state?.lastStatus;
    const consecutiveErrors = job.state?.consecutiveErrors ?? 0;
    return job.enabled && consecutiveErrors >= 1 && lastRunStatus === "error";
  }).length;
}

// An enabled job whose scheduled next run is already in the past is "overdue"
// — the container hasn't fired it on time. nowMs is injected so tests can
// freeze the clock. A job that has already fired and is still executing has a
// past nextRunAtMs but a live runningAtMs; it is in-progress, not overdue, so
// it's excluded here. runningAtMs is JobCard's own running indicator, so the
// badge and the per-job card agree on what "running" means.
export function countCronOverdue(jobs: CronJob[], nowMs: number): number {
  return jobs.filter((job) => {
    const next = job.state?.nextRunAtMs;
    const isRunning = Boolean(job.state?.runningAtMs);
    return job.enabled && !isRunning && next != null && next < nowMs;
  }).length;
}
