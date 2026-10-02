// Ported from upstream Paperclip's external-objects.ts and the external-object
// maps in status-colors.ts (paperclip/ui/src/lib/external-objects.ts,
// paperclip/ui/src/lib/status-colors.ts) (MIT, (c) 2025 Paperclip AI).
// Colours are our tokens: each category resolves to one CSS variable.

import {
  Archive,
  CheckCircle2,
  Circle,
  CircleDashed,
  CircleDot,
  Clock,
  CloudOff,
  GitMerge,
  GitPullRequest,
  Github,
  KeyRound,
  Loader2,
  OctagonAlert,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import type { ExternalObjectSummary } from "@/components/teams/shared/types";

/**
 * Lucide icon for each status category. The mapping is host-owned: providers
 * never inject inline React.
 *
 * Both icon tables are exported as tables, not behind upstream's lookup
 * functions: a component returned from a call inside render trips the
 * react-hooks/static-components lint, a property read does not. Read them as
 * `externalObjectIconsByKey[key ?? ""] ?? externalObjectIconsByCategory[category] ?? CircleDashed`.
 */
export const externalObjectIconsByCategory: Partial<Record<string, LucideIcon>> = {
  unknown: CircleDashed,
  open: CircleDot,
  waiting: Clock,
  running: Loader2,
  succeeded: CheckCircle2,
  failed: XCircle,
  blocked: OctagonAlert,
  closed: Circle,
  archived: Archive,
  auth_required: KeyRound,
  unreachable: CloudOff,
};

export const externalObjectIconDefault: LucideIcon = CircleDashed;

export const externalObjectIconsByKey: Partial<Record<string, LucideIcon>> = {
  archive: Archive,
  check: CheckCircle2,
  "check-circle": CheckCircle2,
  circle: Circle,
  "circle-dot": CircleDot,
  clock: Clock,
  github: Github,
  "git-merge": GitMerge,
  "git-pull-request": GitPullRequest,
  key: KeyRound,
  loader: Loader2,
  "x-circle": XCircle,
};

// Upstream's tone table, mapped onto our ladder:
//   unknown / closed / archived      -> --fg-mute  (upstream muted / neutral-500)
//   open / running                   -> --info     (upstream blue)
//   waiting / auth_required          -> --warn     (upstream amber)
//   succeeded                        -> --live     (upstream green)
//   failed / blocked / unreachable   -> --alert    (upstream red)
//   merged                           -> --fg       (upstream violet; we have no purple, so
//                                       it takes the colour a done task has in StatusIcon
//                                       and stays distinct from an open PR's --info)
const CATEGORY_COLORS: Record<string, string> = {
  unknown: "var(--fg-mute)",
  open: "var(--info)",
  waiting: "var(--warn)",
  running: "var(--info)",
  succeeded: "var(--live)",
  merged: "var(--fg)",
  failed: "var(--alert)",
  blocked: "var(--alert)",
  closed: "var(--fg-mute)",
  archived: "var(--fg-mute)",
  auth_required: "var(--warn)",
  unreachable: "var(--alert)",
};

export function externalObjectColor(category: string): string {
  return CATEGORY_COLORS[category] ?? "var(--fg-mute)";
}

/**
 * Liveness overlay applied on top of the status colour: a dashed border (and
 * reduced opacity when stale) is the non-colour differentiator for
 * stale / auth_required / unreachable.
 */
export const externalObjectLivenessOverlay: Record<string, string> = {
  unknown: "",
  fresh: "",
  stale: "opacity-70 [border-style:dashed]",
  auth_required: "[border-style:dashed]",
  unreachable: "[border-style:dashed]",
};

const CATEGORY_LABELS: Record<string, string> = {
  unknown: "Not yet resolved",
  open: "Open",
  waiting: "Waiting",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  blocked: "Blocked",
  closed: "Closed",
  archived: "Archived",
  auth_required: "Authorization required",
  unreachable: "Unreachable",
};

export function externalObjectCategoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? category.replace(/_/g, " ");
}

const LIVENESS_LABELS: Record<string, string> = {
  unknown: "Not yet refreshed",
  fresh: "Fresh",
  stale: "Stale",
  auth_required: "Requires auth",
  unreachable: "Unreachable",
};

export function externalObjectLivenessLabel(liveness: string): string {
  return LIVENESS_LABELS[liveness] ?? liveness.replace(/_/g, " ");
}

export function externalObjectDisplayStatusLabel(input: {
  providerKey: string | null | undefined;
  objectType: string | null | undefined;
  statusCategory: string;
  liveness: string;
  statusLabel?: string | null;
}): string {
  const trimmedStatusLabel = input.statusLabel?.trim();
  if (trimmedStatusLabel) return trimmedStatusLabel;
  const isGenericUrl = input.providerKey === "url" && input.objectType === "link";
  const hasKnownObjectType = Boolean(input.providerKey && input.objectType);
  if (input.statusCategory === "unknown" && hasKnownObjectType && !isGenericUrl) {
    if (input.liveness === "fresh") return "Status unavailable";
    return externalObjectLivenessLabel(input.liveness);
  }
  return externalObjectCategoryLabel(input.statusCategory);
}

/** Higher number = more attention-worthy. Rollups sort by tone first. */
const TONE_SEVERITY: Record<string, number> = {
  muted: 0,
  neutral: 1,
  success: 2,
  info: 3,
  warning: 4,
  danger: 5,
};

export function externalObjectToneSeverity(tone: string | null | undefined): number {
  if (!tone) return 0;
  return TONE_SEVERITY[tone] ?? 0;
}

const PROVIDER_LABELS: Record<string, string> = {
  github: "GitHub",
  github_pull_request: "GitHub",
  github_issue: "GitHub",
  hubspot: "HubSpot",
  linear: "Linear",
  jira: "Jira",
  notion: "Notion",
  asana: "Asana",
};

export function externalObjectProviderLabel(providerKey: string | null | undefined): string {
  if (!providerKey) return "External";
  const lookup = PROVIDER_LABELS[providerKey];
  if (lookup) return lookup;
  return providerKey
    .split(/[._-]/)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

const OBJECT_TYPE_LABELS: Record<string, string> = {
  pull_request: "pull request",
  issue: "issue",
  deployment: "deployment",
  workflow_run: "workflow run",
  ticket: "ticket",
  lead: "lead",
  url_link: "URL",
};

export function externalObjectTypeLabel(objectType: string | null | undefined): string {
  if (!objectType) return "object";
  return OBJECT_TYPE_LABELS[objectType] ?? objectType.replace(/_/g, " ");
}

export function externalObjectDisplayLabel(
  providerKey: string | null | undefined,
  objectType: string | null | undefined,
  displayKey?: string | null,
): string {
  const trimmedDisplayKey = displayKey?.trim();
  if (trimmedDisplayKey) return trimmedDisplayKey;
  if (providerKey === "url" && objectType === "link") return "URL";
  return `${externalObjectProviderLabel(providerKey)} ${externalObjectTypeLabel(objectType)}`;
}

/** "PR 12" / "Issue 12" for a github.com pull or issue URL, else null. */
export function githubObjectLabel(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (parsed.hostname !== "github.com") return null;
    const [, owner, repo, kind, number] = parsed.pathname.split("/");
    if (!owner || !repo || !number) return null;
    if (kind === "pull") return `PR ${number}`;
    if (kind === "issues") return `Issue ${number}`;
    return null;
  } catch {
    return null;
  }
}

/**
 * The dominant tone in a summary, used by list rollups. `null` when there are
 * no objects or every tone is `muted`.
 */
export function dominantExternalObjectTone(
  summary: Pick<ExternalObjectSummary, "highestSeverity" | "objects"> | null | undefined,
): ExternalObjectSummary["highestSeverity"] | null {
  if (!summary) return null;
  const tone = summary.highestSeverity;
  if (!tone) return null;
  if (externalObjectToneSeverity(tone) <= TONE_SEVERITY.muted) return null;
  return tone;
}

/**
 * The count of objects matching the dominant severity (e.g. "3 failed PRs"),
 * not the global total. 0 whenever the dominant tone is muted.
 */
export function externalObjectDominantCount(
  summary: Pick<ExternalObjectSummary, "highestSeverity" | "objects"> | null | undefined,
): number {
  if (!summary) return 0;
  const tone = dominantExternalObjectTone(summary);
  if (!tone) return 0;
  return summary.objects.filter((object) => object.statusTone === tone).length;
}
