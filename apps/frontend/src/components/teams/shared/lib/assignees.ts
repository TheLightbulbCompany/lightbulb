// apps/frontend/src/components/teams/shared/lib/assignees.ts

// Ported from upstream Paperclip's assignees.ts (paperclip/ui/src/lib/assignees.ts)
// (MIT, (c) 2025 Paperclip AI). Subset retained for IssueRow /
// IssueFiltersPopover. Upstream's User parameter was already a bare userId
// string, so no Isol8 CompanyMember swap is required.

import type { CompanyMember } from "../types";

/** Members who can own a task, keyed by the Paperclip user id an issue's
 * `assigneeUserId` holds (not the Clerk id). One without a Paperclip identity
 * is left out: Paperclip would refuse the assignment. */
export function assignableMembers(members: readonly CompanyMember[]): CompanyMember[] {
  return members.flatMap((m) => (m.paperclipUserId ? [{ ...m, userId: m.paperclipUserId }] : []));
}

/** The signed-in member's Paperclip user id (what an issue's `assigneeUserId`
 * holds), from the `/members` roster and their Clerk id. Null until known. */
export function paperclipUserIdOf(
  members: readonly CompanyMember[] | undefined,
  clerkUserId: string | null | undefined,
): string | null {
  if (!clerkUserId) return null;
  return members?.find((m) => m.userId === clerkUserId)?.paperclipUserId ?? null;
}

export interface AssigneeSelection {
  assigneeAgentId: string | null;
  assigneeUserId: string | null;
}

export interface AssigneeOption {
  id: string;
  label: string;
  searchText?: string;
}

export function assigneeValueFromSelection(selection: Partial<AssigneeSelection>): string {
  if (selection.assigneeAgentId) return `agent:${selection.assigneeAgentId}`;
  if (selection.assigneeUserId) return `user:${selection.assigneeUserId}`;
  return "";
}

export function parseAssigneeValue(value: string): AssigneeSelection {
  if (!value) {
    return { assigneeAgentId: null, assigneeUserId: null };
  }
  if (value.startsWith("agent:")) {
    const assigneeAgentId = value.slice("agent:".length);
    return { assigneeAgentId: assigneeAgentId || null, assigneeUserId: null };
  }
  if (value.startsWith("user:")) {
    const assigneeUserId = value.slice("user:".length);
    return { assigneeAgentId: null, assigneeUserId: assigneeUserId || null };
  }
  // Backward compatibility for older drafts/defaults that stored a raw agent id.
  return { assigneeAgentId: value, assigneeUserId: null };
}

export function currentUserAssigneeOption(currentUserId: string | null | undefined): AssigneeOption[] {
  if (!currentUserId) return [];
  return [{
    id: assigneeValueFromSelection({ assigneeUserId: currentUserId }),
    label: "Me",
    searchText: currentUserId === "local-board" ? "me board human local-board" : `me human ${currentUserId}`,
  }];
}

export function formatAssigneeUserLabel(
  userId: string | null | undefined,
  currentUserId: string | null | undefined,
  userLabels?: ReadonlyMap<string, string> | Record<string, string> | null,
): string | null {
  if (!userId) return null;
  if (currentUserId && userId === currentUserId) return "You";
  if (userLabels) {
    const label = userLabels instanceof Map
      ? userLabels.get(userId)
      : (userLabels as Record<string, string>)[userId];
    if (typeof label === "string" && label.trim()) return label;
  }
  if (userId === "local-board") return "Board";
  // No slicing the raw id as a last resort — the first 5 chars of a Clerk id
  // are literally "user_". Callers pick their own generic fallback on null.
  return null;
}
