import { ApiError, type ApiMethods } from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";

import { relativeTime } from "./relativeTime";

// Email invitations and the join link, the two ways into a workspace
// (apps/backend/routers/orgs.py). Every route is admin-only: a member gets 403.

export type Invitation = { id: string; email: string; created_at: string; expires_at: string | null; status: string };

const orgPath = (orgId: string) => `/orgs/${encodeURIComponent(orgId)}`;

export async function listInvitations(api: ApiMethods, orgId: string): Promise<Invitation[]> {
  const body = (await api.get(`${orgPath(orgId)}/invitations`)) as { invitations?: Invitation[] };
  return body.invitations ?? [];
}

export async function sendInvitation(api: ApiMethods, orgId: string, email: string): Promise<Invitation> {
  return (await api.post(`${orgPath(orgId)}/invitations`, { email })) as Invitation;
}

export async function revokeInvitation(api: ApiMethods, orgId: string, id: string): Promise<void> {
  await api.del(`${orgPath(orgId)}/invitations/${encodeURIComponent(id)}`);
}

export const ADMIN_ONLY_TEXT = "Only a workspace admin can invite people.";

export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

/** One line per address: the server's 409 wording as given, else a fixed one. */
export function inviteResultText(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = (error.body as { detail?: unknown } | null)?.detail;
    if (error.status === 409 && typeof detail === "string") return detail;
    if (error.status === 422) return "That doesn't look like an email address.";
    if (error.status === 403) return ADMIN_ONLY_TEXT;
  }
  return "Couldn't send. Try again.";
}

/** "a@x.com, b@y.com c@z.com" → each address once. */
export function splitEmails(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).filter(Boolean))];
}

/** "Sent 2h ago" from the invitation's ISO `created_at`. */
export function sentAgo(createdAt: string, now: number = Date.now()): string {
  const age = relativeTime(Date.parse(createdAt), now);
  if (!age) return "";
  if (age === "now") return "Sent just now";
  return /^\d+[mhd]$/.test(age) ? `Sent ${age} ago` : `Sent ${age}`;
}

/**
 * The workspace's join link, minted only when none exists: POST is
 * revoke-and-regenerate, so it must never strand a link people hold. null =
 * a legacy link exists but its URL can't be shown (only a hash is stored).
 */
export async function getJoinLink(api: ApiMethods, orgId: string): Promise<string | null> {
  const path = `${orgPath(orgId)}/join-link`;
  const existing = (await api.get(path)) as { exists?: boolean; join_url?: string | null };
  if (existing?.exists) return existing.join_url || null;
  return ((await api.post(path, {})) as { join_url: string }).join_url;
}

export const LEGACY_JOIN_LINK_TEXT =
  "This workspace's link can't be shown here. Regenerate it on the web under Settings → Members.";

/** Copies the join link; the status line to show. Throws when the request fails. */
export async function copyJoinLink(api: ApiMethods, orgId: string): Promise<string> {
  const url = await getJoinLink(api, orgId);
  if (!url) return LEGACY_JOIN_LINK_TEXT;
  await Clipboard.setStringAsync(url);
  return "Join link copied.";
}
