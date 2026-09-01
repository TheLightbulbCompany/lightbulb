// Preset automations — the catalog result.dev renders as "Nothing running yet
// / Pick one to switch on": eight named things, one sentence each, a toggle,
// and a `Needs setup` label when a dependency is missing.
//
// THIS IS A LAYER ON TOP OF THE BUILDER, NOT A REPLACEMENT. Owner constraint
// 2026-09-01: the way Automations is built matters for functionality — it
// rides Paperclip routines, webhook triggers with signing modes, and
// connected-app triggers with provider-side registration — so none of that
// changes. A preset is nothing more than the two calls `CreateRoutineDialog`
// already makes, with the values filled in:
//
//   POST /routines                 { title, description, assignee_agent_id, status }
//   POST /routines/{id}/triggers   { kind: "schedule", cron_expression, timezone }
//
// Every routine a preset creates is a plain Paperclip routine afterwards —
// it appears in the same table, opens the same detail panel, and can be
// edited, paused or deleted through the builder exactly like a hand-made one.
//
// SCHEDULE-ONLY, DELIBERATELY. Webhook and connected-app triggers need
// provider-side registration (Stripe's is a guided paste of a signing secret;
// GitHub's auto-registers a hook) and a finish dialog — that is what the
// builder's other two rungs are for, and a one-click preset cannot honestly
// do it. So "watch for failed payments" is not here; it is a builder job.
//
// `requires` names connection-rail providers the routine's INSTRUCTIONS
// depend on. Absent ⇒ "Needs setup", and the switch is disabled with the
// reason shown — never a routine that runs and fails on its first turn.

export interface RoutinePreset {
  /** Stable id, also used to recognise an already-created preset by title. */
  key: string;
  title: string;
  /** One plain sentence: exactly what this does, in the owner's words. */
  summary: string;
  /** What the assigned agent is told each run. */
  description: string;
  cron: string;
  /** Human reading of `cron`, so nobody has to parse it. */
  cadence: string;
  /** Connection-rail provider keys the instructions depend on. */
  requires: string[];
}

export const ROUTINE_PRESETS: RoutinePreset[] = [
  {
    key: "inbox-triage",
    title: "Check the company inbox",
    summary: "Reads new mail each morning, replies to anything routine, and flags what needs you.",
    description:
      "Read every new message in the company inbox since your last run. Reply to anything routine (scheduling, simple questions, acknowledgements). For anything that needs a decision, a payment, or a promise, do not reply — file a task for me with the thread summarised and your suggested response.",
    cron: "0 8 * * 1-5",
    cadence: "Weekday mornings",
    requires: [],
  },
  {
    key: "quiet-tasks",
    title: "Follow up on quiet tasks",
    summary: "Finds tasks nobody has touched in a week and either moves them or asks you.",
    description:
      "List every open task with no activity in the last 7 days. For each: if it is clearly done, close it with a note; if it is blocked, say on what and ask me; if it just stalled, do the next concrete step and leave a comment saying what you did.",
    cron: "0 9 * * 1",
    cadence: "Monday mornings",
    requires: [],
  },
  {
    key: "site-health",
    title: "Check your site",
    summary: "Runs a health check on your website and files the top fixes as tasks.",
    description:
      "Open our website in the browser and audit it: broken links, missing titles and descriptions, slow pages, anything a first-time visitor would trip on. File the three most important fixes as tasks with a one-line reason each. If nothing needs fixing, say so and stop.",
    cron: "0 7 * * 1",
    cadence: "Monday mornings",
    requires: [],
  },
  {
    key: "weekly-money",
    title: "Weekly money summary",
    summary: "Every Friday: what came in, what went out, and anything that looks off.",
    description:
      "Summarise the company's Stripe activity for the past 7 days: revenue, refunds, disputes, new customers, and failed payments. Compare to the week before in one line. Flag anything unusual. Keep it under ten lines.",
    cron: "0 16 * * 5",
    cadence: "Friday afternoons",
    requires: ["stripe"],
  },
  {
    key: "post-instagram",
    title: "Post to Instagram",
    summary: "Drafts and posts one update a week about what the company shipped.",
    description:
      "Look at what the company shipped or learned this week — closed tasks, new customers, anything in the Brain. Write one Instagram post about the most interesting thing, in our voice, with a short caption. Post it. Leave a comment here with a link to the post.",
    cron: "0 11 * * 3",
    cadence: "Wednesday late morning",
    requires: ["instagram"],
  },
  {
    key: "post-x",
    title: "Post to X",
    summary: "One post a week on X about what the company shipped or learned.",
    description:
      "Look at what the company shipped or learned this week. Write one post for X about the most interesting thing — short, specific, no hashtags. Post it. Leave a comment here with a link to the post.",
    cron: "0 11 * * 2",
    cadence: "Tuesday late morning",
    requires: ["x"],
  },
];

/** Stamped into the routine's description on create, so the preset can be
 *  recognised later by something the owner does not edit by accident.
 *
 *  NOT the title: titles are editable in RoutineDetailPanel, so a renamed
 *  preset would be offered again (and switched on twice), while a hand-made
 *  routine that happened to share a title would hide the preset. A routine
 *  has no metadata/tags field to carry identity, so the description — which
 *  the agent reads as its instructions — carries one trailing marker line. */
export function presetMarker(key: string): string {
  return `[lightbulb-preset:${key}]`;
}

/** A preset is "on" when any existing routine's instructions carry its marker. */
export function presetIsOn(preset: RoutinePreset, routineDescriptions: Iterable<string>): boolean {
  const marker = presetMarker(preset.key);
  for (const d of routineDescriptions) if (d.includes(marker)) return true;
  return false;
}
