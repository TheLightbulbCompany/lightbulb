/**
 * Shared onboarding/provisioning copy + timing, consumed by BOTH the web
 * ProvisioningStepper and the mobile ProvisioningStepper so the two surfaces
 * stay in lockstep. Pure values — no React, no platform APIs, no imports.
 */

/** Fargate cold starts (image pull from ECR + ENI attach + container init)
 *  legitimately run 3-4 min for a service's first task; the elapsed counter
 *  softens its copy only past this budget. */
export const PROVISION_TIMEOUT_MS = 300_000;

/** Second, harder tier. Past this the copy stops claiming progress ("still
 *  going") and says plainly that something is stuck on our side. 3x the
 *  cold-start budget above, comfortably past any legitimate Fargate first
 *  task. A dev owner once sat on the first tier's copy for 21 HOURS. */
export const PROVISION_STUCK_MS = 900_000;

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

/** m:ss for the elapsed provisioning counter, rolling to h:mm:ss past an
 *  hour. It used to be mm:ss with no rollover, so a stall that ran 21 hours
 *  rendered as "1260:37" — a four-digit minute count sitting next to the
 *  word "minutes". */
export function formatProvisionElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor(total / 60) % 60;
  const ss = (total % 60).toString().padStart(2, "0");
  return h > 0 ? `${h}:${m.toString().padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
