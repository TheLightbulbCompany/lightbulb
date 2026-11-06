// Starter schedules for a project's empty Scheduled view (UX slice 6). A tap only drafts the ask in the project's
// composer: nothing is created until the person sends it and turns it on in the Router's confirm card. Times are said
// as clock times, which the Router reads in the viewer's time zone. A project with a goal gets three built from it
// (W13-13), the way its starter asks are; one without gets the roster's.

import { goalLine } from "@/components/messaging/RoomIntro";

export interface ScheduleTemplate {
  /** The specialist's roster name ("Growth"); "Lightbulb" for the fallback. */
  specialist: string;
  name: string;
  cadence: string;
  draft: string;
}

export const SCHEDULE_TEMPLATES: readonly ScheduleTemplate[] = [
  { specialist: "Growth", name: "Weekly signups", cadence: "Mondays 9:00 AM", draft: "Every Monday at 9:00 AM, have Growth post last week's signups here." },
  { specialist: "Customer Ops", name: "Unanswered customer email", cadence: "Weekdays 9:00 AM", draft: "Every weekday at 9:00 AM, have Customer Ops post any customer email still waiting on a reply here." },
  { specialist: "Finance", name: "Weekly cash summary", cadence: "Mondays 8:00 AM", draft: "Every Monday at 8:00 AM, have Finance post last week's money in and out here." },
  { specialist: "Analytics", name: "Weekly traffic", cadence: "Mondays 10:00 AM", draft: "Every Monday at 10:00 AM, have Analytics post last week's site traffic and top pages here." },
  { specialist: "Engineering", name: "Site health check", cadence: "Daily 8:00 AM", draft: "Every day at 8:00 AM, have Engineering check the site is up and post here only if something is wrong." },
];
export const LIGHTBULB_TEMPLATE: ScheduleTemplate = {
  specialist: "Lightbulb", name: "Weekly project summary", cadence: "Fridays 4:00 PM", draft: "Every Friday at 4:00 PM, post a short summary of this week's work here.",
};

/** Three cards built from a project's goal; none without one. */
export function goalTemplates(topic: string | null | undefined): ScheduleTemplate[] {
  const goal = goalLine(topic);
  if (!goal) return [];
  return [
    { specialist: "Lightbulb", name: "Monday plan", cadence: "Mondays 9:00 AM",
      draft: `Every Monday at 9:00 AM, post this week's next steps toward "${goal}" here, and anything that's in the way.` },
    { specialist: "Lightbulb", name: "Friday wrap-up", cadence: "Fridays 4:00 PM",
      draft: `Every Friday at 4:00 PM, post a short summary of this week's progress on "${goal}" here.` },
    { specialist: "Lightbulb", name: "What others are doing", cadence: "1st of the month 10:00 AM",
      draft: `On the 1st of every month at 10:00 AM, look at how others are doing this ("${goal}") and post anything worth trying here.` },
  ];
}

/** Three cards: the first two specialists on the roster that have a template, then Lightbulb's. Before the roster
 *  loads (no names), the first two templates stand in. */
export function pickTemplates(rosterNames: Iterable<string>): ScheduleTemplate[] {
  // By first word, so "Customer Ops" and "Customer Operations" both match.
  const word = (n: string) => n.trim().split(/[\s_]+/)[0].toLowerCase();
  const names = new Set([...rosterNames].map(word));
  const own = names.size ? SCHEDULE_TEMPLATES.filter((t) => names.has(word(t.specialist))) : SCHEDULE_TEMPLATES;
  return [...own.slice(0, 2), LIGHTBULB_TEMPLATE];
}
