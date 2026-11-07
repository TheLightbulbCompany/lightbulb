/**
 * The workspace roster reads (`/teams/agents`, `/teams/members`), bearer-only.
 *
 * getTokenRef pattern (MANDATORY): Clerk hands out a NEW `getToken` reference every render. Pinning it in a ref
 * keeps the adapter referentially stable so focus effects fire once per focus. Do NOT put `[getToken]` in any
 * dependency array.
 */
import { useAuth } from "@clerk/expo";
import { useMemo, useRef } from "react";
import { fetchJsonWithStatus, type PlatformAdapter } from "@lightbulb/shared";

import { makeAdapter } from "./adapter";

/** An agent from `GET /teams/agents` (`{agents:[{…,seeded}]}`, adapter redacted). */
export interface TeamsAgent {
  id: string;
  name?: string | null;
  seeded?: boolean;
  /** Provenance stamp: a fresh workspace's starter roster is `"Starter"` with `seeded: false`. */
  origin?: string | null;
  /** Paperclip's agent status, verbatim (running / idle / paused / error …): the avatar ring. */
  status?: string | null;
  role?: string | null;
  /** The manager's agent id; the root CEO has none. */
  reportsTo?: string | null;
}

/**
 * A company member from `GET /teams/members` (`{members:[…]}`, contract §1).
 * `userId` is the **Clerk** user id; the BFF joins principal→Clerk and resolves
 * name/email/imageUrl. `name` already falls back full-name → email → id upstream,
 * so it is always a usable label. Powers human-assignee + comment-author names.
 */
export interface CompanyMember {
  userId: string;
  /** The member's Paperclip user id (what a saved document's `updatedByUserId` carries). */
  paperclipUserId?: string | null;
  name?: string | null;
  email?: string | null;
  imageUrl?: string | null;
}

/** Build a stable ApiClientConfig around a ref-pinned getToken. */
export function useTeamsConfig(): PlatformAdapter {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  return useMemo(() => makeAdapter(() => getTokenRef.current()), []);
}

/**
 * Best-effort `GET /teams/agents` → `TeamsAgent[]`. A failure resolves to `[]`
 * so the caller's primary (gate-bearing) data is never blanked by a name-source
 * hiccup. Shared by the board + issue-detail loads.
 */
export function fetchAgents(config: PlatformAdapter): Promise<TeamsAgent[]> {
  return fetchJsonWithStatus(config, "/teams/agents")
    .then((r) => {
      const list = (r.body as { agents?: unknown } | null)?.agents;
      return Array.isArray(list) ? (list as TeamsAgent[]) : [];
    })
    .catch(() => [] as TeamsAgent[]);
}

/**
 * Best-effort `GET /teams/members` → `CompanyMember[]` (contract §1). Same
 * best-effort posture as {@link fetchAgents}: a members-fetch failure must NOT
 * blank the board/detail — it just degrades human names to their id fallback.
 */
export function fetchMembers(config: PlatformAdapter): Promise<CompanyMember[]> {
  return fetchJsonWithStatus(config, "/teams/members")
    .then((r) => {
      const list = (r.body as { members?: unknown } | null)?.members;
      return Array.isArray(list) ? (list as CompanyMember[]) : [];
    })
    .catch(() => [] as CompanyMember[]);
}
