import { describeCron } from "./cron-readable";

/** Enough of a trigger to describe — matches the fields both
 * `RoutineTriggerSummary` (detail) and the list row's `RoutineTriggerLite`
 * carry. */
export interface TriggerSentenceInput {
  kind: string;
  cronExpression?: string | null;
}

/**
 * Plain-English "what starts this" sentence for a routine trigger — the
 * legibility layer that makes a routine read as an automation instead of a
 * raw cron string or trigger-kind enum.
 *
 * Covers the trigger kinds the backend actually creates
 * (`CreateRoutineTriggerBody.kind: schedule|webhook|api` — see
 * routers/teams/schemas.py). `api` triggers have no create UI yet but are a
 * real persisted kind, so they still need a sentence.
 */
export function triggerSentence(trigger: TriggerSentenceInput): string {
  switch (trigger.kind) {
    case "schedule":
      return `on a schedule — ${describeCron(trigger.cronExpression ?? "")}`;
    case "webhook":
      return "when an app calls this webhook";
    case "api":
      return "when triggered by the API";
    default:
      return `when triggered by ${trigger.kind}`;
  }
}
