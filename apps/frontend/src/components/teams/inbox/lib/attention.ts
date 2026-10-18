// apps/frontend/src/components/teams/inbox/lib/attention.ts

// Ported from upstream Paperclip's attention helpers
// (paperclip/ui/src/lib/attention.ts @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). Source labels, kind -> borrowed task status,
// task ref, detail line, the desk shelves (UTC day boundaries, in lockstep with
// the server), aging and the decide-by label are verbatim, except where listed.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared types -> components/teams/shared/types (desk fields optional)
//   - attentionTaskRef also returns the issue id: shell links are built from it
//   - INLINE_RESOLVABLE_SOURCE_KINDS -> approval, decision, issue_thread_interaction:
//       join requests and stalled-review verbs have no BFF route, so those rows
//       deep-link (Open) instead; an interaction is inline only when the BFF
//       hydrated it (the card needs the full row)
//   - attentionImageUrl / attentionDetailImages -> not ported: /api/assets is not
//       proxied for attention, so rows carry no thumbnails
//   - toolbar group/sort/filter + localStorage prefs, date-range chips, the
//       incremental render plan and the triage decide-by options -> not ported
//       (the desk renders the default ungrouped view)

import type { AttentionItem, AttentionSourceKind } from "@/components/teams/shared/types";

export const INLINE_RESOLVABLE_SOURCE_KINDS: ReadonlySet<AttentionSourceKind> = new Set<AttentionSourceKind>([
  "approval",
  "decision",
  "issue_thread_interaction",
]);

export function isInlineResolvable(item: AttentionItem): boolean {
  if (!item.inlineResolvable || !INLINE_RESOLVABLE_SOURCE_KINDS.has(item.sourceKind)) return false;
  if (item.sourceKind === "issue_thread_interaction") return Boolean(item.interaction);
  return true;
}

const SOURCE_LABEL: Record<AttentionSourceKind, string> = {
  approval: "Approval",
  decision: "Decision",
  issue_thread_interaction: "Decision requested",
  join_request: "Join request",
  recovery_action: "Recovery",
  productivity_review: "Task",
  blocker_attention: "Blocked dependency",
  review: "Review",
  failed_run: "Failed run",
  budget_alert: "Budget",
  agent_error_alert: "Agent error",
};

export function sourceLabel(kind: AttentionSourceKind): string {
  return SOURCE_LABEL[kind] ?? kind.replaceAll("_", " ");
}

/** The task status a row borrows its glyph and colour from: stuck, or waiting on a verdict. */
export function attentionStatus(item: AttentionItem): "blocked" | "in_review" {
  switch (item.sourceKind) {
    case "failed_run":
    case "agent_error_alert":
    case "blocker_attention":
    case "recovery_action":
    case "budget_alert":
      return "blocked";
    default:
      return "in_review";
  }
}

/** The task a row belongs to: `relatedIssue` when present, else an issue subject. */
export function attentionTaskRef(item: AttentionItem): { id: string; identifier: string; href: string | null } | null {
  const related = item.relatedIssue;
  if (related?.identifier) return { id: related.id, identifier: related.identifier, href: related.href };
  const subject = item.subject;
  if (subject.kind === "issue" && subject.identifier) {
    return { id: subject.id, identifier: subject.identifier, href: subject.href };
  }
  return null;
}

function quote(text: string | null | undefined): string | null {
  const trimmed = text?.trim();
  if (!trimmed) return null;
  return `“${trimmed}”`;
}

function countNoun(count: number, singular: string): string {
  return `${count} ${count === 1 ? singular : `${singular}s`}`;
}

/** One secondary line from the structured `detail`; null falls back to `whyNow`. */
export function attentionDetailLine(item: AttentionItem): string | null {
  const detail = item.detail;
  if (!detail) return null;
  switch (detail.kind) {
    case "plan_approval":
      return detail.planTitle?.trim() || quote(detail.summaryExcerpt);
    case "approval":
      return quote(detail.summaryExcerpt);
    case "confirmation":
      return quote(detail.promptExcerpt);
    case "checkbox_confirmation": {
      const q = quote(detail.promptExcerpt);
      return q ? `${countNoun(detail.optionCount, "option")} — ${q}` : countNoun(detail.optionCount, "option");
    }
    case "questions": {
      const q = quote(detail.firstQuestionText);
      const label = countNoun(detail.questionCount, "question");
      return q ? `${label} — ${q}` : label;
    }
    case "suggested_tasks": {
      const q = quote(detail.firstTaskTitle);
      const label = countNoun(detail.taskCount, "suggested task");
      return q ? `${label} — ${q}` : label;
    }
    case "item_verdicts": {
      const q = quote(detail.promptExcerpt);
      const label = `${countNoun(detail.itemCount, "item")} to verdict`;
      return q ? `${label} — ${q}` : label;
    }
    case "failed_run":
    case "agent_error": {
      const reason = quote(detail.failureReasonExcerpt);
      if (detail.agentName && reason) return `${detail.agentName} — ${reason}`;
      return detail.agentName ?? reason;
    }
    case "blocker": {
      const b = detail.blockingIssue;
      if (!b) return null;
      const id = b.identifier ? `${b.identifier} ` : "";
      return b.title ? `Blocked by ${id}${b.title}` : b.identifier ? `Blocked by ${b.identifier}` : null;
    }
    case "budget":
      return `${Math.round(detail.observedPercent)}% of budget used ($${detail.amountObserved} / $${detail.amountLimit})`;
    case "generic":
      return quote(detail.summaryExcerpt);
    default:
      return null;
  }
}

// --- Today's desk ----------------------------------------------------------
// "Decide now" (explicit decide-by due today/past), "New today" (surfaced
// today), "Earlier". UTC day boundaries, matching the server's badge.

const MS_PER_DAY = 24 * 60 * 60 * 1_000;

function startOfUtcDay(now: number): number {
  const value = new Date(now);
  return Date.UTC(value.getUTCFullYear(), value.getUTCMonth(), value.getUTCDate());
}

function endOfUtcDay(now: number): number {
  return startOfUtcDay(now) + MS_PER_DAY - 1;
}

function endOfUtcWeek(now: number): number {
  const start = startOfUtcDay(now);
  const weekday = new Date(start).getUTCDay();
  const daysUntilSunday = weekday === 0 ? 0 : 7 - weekday;
  return start + (daysUntilSunday + 1) * MS_PER_DAY - 1;
}

/** [bucket, deadline]: lower bucket / earlier deadline sorts first. */
export function attentionDecideOrder(item: AttentionItem, now: number): [number, number] {
  const decideBy = item.decideBy ?? null;
  if (decideBy === "today") return [0, endOfUtcDay(now)];
  if (decideBy === "this_week") return [0, endOfUtcWeek(now)];
  if (decideBy && /^\d{4}-\d{2}-\d{2}$/.test(decideBy)) {
    const deadline = Date.parse(`${decideBy}T23:59:59.999Z`);
    if (Number.isFinite(deadline)) return [0, deadline];
  }
  if (decideBy === "whenever") return [1, Number.MAX_SAFE_INTEGER];
  return [2, Number.MAX_SAFE_INTEGER];
}

export function attentionIsDecideNow(item: AttentionItem, now: number): boolean {
  const [bucket, deadline] = attentionDecideOrder(item, now);
  return bucket === 0 && deadline <= endOfUtcDay(now);
}

export function attentionIsNewToday(item: AttentionItem, now: number): boolean {
  const ts = new Date(item.createdAt).getTime();
  return Number.isFinite(ts) && ts >= startOfUtcDay(now);
}

export interface DeskShelf {
  key: string;
  label: string;
  items: AttentionItem[];
}

const rankOf = (item: AttentionItem) => item.rank ?? 0;

function arrival(item: AttentionItem): number {
  const ts = new Date(item.createdAt).getTime();
  return Number.isFinite(ts) ? ts : 0;
}

/** The default desk: Decide now (only when something is due), New today, Earlier. Disjoint. */
export function buildDeskShelves(items: AttentionItem[], now: number): DeskShelf[] {
  const decideNow = items
    .filter((item) => attentionIsDecideNow(item, now))
    .sort((a, b) => {
      const [, aDeadline] = attentionDecideOrder(a, now);
      const [, bDeadline] = attentionDecideOrder(b, now);
      if (aDeadline !== bDeadline) return aDeadline - bDeadline;
      return rankOf(a) - rankOf(b);
    });
  const rest = items
    .filter((item) => !attentionIsDecideNow(item, now))
    .sort((a, b) => arrival(b) - arrival(a) || rankOf(a) - rankOf(b));
  const newToday = rest.filter((item) => attentionIsNewToday(item, now));
  const earlier = rest.filter((item) => !attentionIsNewToday(item, now));

  const shelves: DeskShelf[] = [];
  if (decideNow.length > 0) shelves.push({ key: "desk:decide-now", label: "Decide now", items: decideNow });
  if (newToday.length > 0) shelves.push({ key: "desk:new-today", label: "New today", items: newToday });
  if (earlier.length > 0) shelves.push({ key: "desk:earlier", label: "Earlier", items: earlier });
  return shelves;
}

// --- Aging shelf -------------------------------------------------------------

export const ATTENTION_AGING_DAYS = 30;

/** Server-computed shelf membership, including per-queue retention overrides. */
export function attentionIsAging(item: AttentionItem): boolean {
  return item.shelf === true;
}

/** Idle duration in whole days, for the shelf's "Idle N days" label. */
export function attentionIdleDays(item: AttentionItem, now: number): number {
  const ts = new Date(item.activityAt).getTime();
  const idle = Number.isFinite(ts) ? Math.max(0, now - ts) : 0;
  return Math.floor(idle / MS_PER_DAY);
}

/** Human label for any stored `decideBy` value (preset or `YYYY-MM-DD`). */
export function decideByLabel(decideBy: string | null): string {
  if (!decideBy) return "Not set";
  if (decideBy === "today") return "Today";
  if (decideBy === "this_week") return "This week";
  if (decideBy === "whenever") return "Whenever";
  if (/^\d{4}-\d{2}-\d{2}$/.test(decideBy)) {
    const parsed = new Date(`${decideBy}T00:00:00.000Z`);
    return Number.isFinite(parsed.getTime())
      ? parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" })
      : decideBy;
  }
  return decideBy;
}
