/**
 * Shared onboarding/provisioning copy + timing, consumed by BOTH the web
 * ProvisioningStepper and the mobile ProvisioningStepper so the two surfaces
 * stay in lockstep. Pure values — no React, no platform APIs, no imports.
 */

/** Fargate cold starts (image pull from ECR + ENI attach + container init)
 *  legitimately run 3-4 min for a service's first task; the elapsed counter
 *  softens its copy only past this budget. */
export const PROVISION_TIMEOUT_MS = 300_000;

/** Rotating idea prompts shown while the container spins up. Kept short
 *  (≤ ~52 chars) so they fit on one line without layout shift. */
export const PROVISION_IDEAS: readonly string[] = [
  "Draft your weekly status report from your inbox",
  "Triage email and reply in your voice",
  "Summarize yesterday's meetings into action items",
  "Spin up a research brief on any topic",
  "Schedule focus time around your calendar",
  "Turn a messy doc into a clean one-pager",
  "Watch a channel and ping you on what matters",
  "Run a recurring morning briefing for you",
  "Pull metrics and drop them into a shared doc",
  "Review a PR and flag the risky changes",
];

export const PROVISION_IDEA_ROTATION_MS = 3200;

/** mm:ss formatter for the elapsed provisioning counter. */
export function formatProvisionElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}
