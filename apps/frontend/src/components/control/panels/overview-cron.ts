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
// freeze the clock.
export function countCronOverdue(jobs: CronJob[], nowMs: number): number {
  return jobs.filter((job) => {
    const next = job.state?.nextRunAtMs;
    return job.enabled && next != null && next < nowMs;
  }).length;
}
