import "server-only";

import { apiUrl } from "./url";

// ---------------------------------------------------------------------------
// Response shapes
//
// These mirror the backend Pydantic models in apps/backend/routers/admin.py
// + apps/backend/core/services/admin_service.py. Fields are intentionally
// permissive (`unknown` over `any`, optional where the backend may omit) so
// upstream changes degrade gracefully — Server Components can still render.
// ---------------------------------------------------------------------------

export interface AdminMe {
  user_id: string;
  email: string | null;
  is_admin: true;
}

export interface SystemHealth {
  containers?: {
    total?: number;
    running?: number;
    stopped?: number;
    failed?: number;
    [key: string]: unknown;
  };
  upstream?: Record<string, { ok: boolean; latency_ms?: number; error?: string }>;
  background_tasks?: Record<string, { running: boolean; last_run?: string; last_error?: string }>;
  recent_errors?: Array<{ timestamp: string; source: string; message: string }>;
  [key: string]: unknown;
}

export interface AdminAction {
  action_id?: string;
  admin_user_id?: string;
  target_user_id?: string;
  action: string;
  timestamp: string;
  status?: string;
  payload?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ActionsPage {
  items: AdminAction[];
  cursor: string | null;
}

export interface UserDirectoryRow {
  clerk_id: string;
  email: string | null;
  created_at?: number | string | null;
  last_sign_in_at?: number | string | null;
  banned: boolean;
  container_status: string;
  subscription_status: string | null;
  /**
   * Populated when the user belongs to a Clerk org. The list-view
   * container_status is now resolved via owner_id (== org_id for org members)
   * so org rows surface the real container state; this field lets the table
   * render an inline `[org:slug]` hint next to the email so admins can tell
   * at a glance which rows are organizational.
   */
  org?: AdminOrgContext | null;
}

export interface UsersPage {
  users: UserDirectoryRow[];
  cursor: string | null;
  stubbed: boolean;
}

export interface AdminOrgContext {
  id: string;
  slug: string;
  name: string;
  role: string;
}

export interface UserOverview {
  identity: unknown;
  container: unknown;
  billing: unknown;
  usage: unknown;
  /**
   * Populated when the target user belongs to a Clerk org. The DDB rows in
   * container/billing/usage are keyed by ``owner_id == org_id`` in that case,
   * so the dashboard surfaces this as an indigo banner making the provenance
   * explicit to the admin.
   */
  org?: AdminOrgContext | null;
}

export interface AgentSummary {
  agent_id?: string;
  name?: string;
  description?: string;
  [key: string]: unknown;
}

export interface AgentsPage {
  agents: AgentSummary[];
  cursor: string | null;
  container_status: string;
  error?: string;
  org?: AdminOrgContext | null;
}

export interface AgentDetail {
  agent?: unknown;
  sessions?: unknown[];
  skills?: unknown[];
  config_redacted?: unknown;
  error?: string;
  container_status?: string;
}

export interface PosthogTimeline {
  events: unknown[];
  stubbed?: boolean;
  missing?: boolean;
  error?: string | null;
}

export interface LogsPage {
  events: unknown[];
  cursor: string | null;
  missing?: boolean;
  error?: string | null;
}

export interface CloudwatchUrlResponse {
  url: string;
}

export interface AdminApiError {
  error: string;
  status?: number;
}

// ---------------------------------------------------------------------------
// Internal fetcher
// ---------------------------------------------------------------------------

interface FetchOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  query?: Record<string, string | number | undefined | null>;
  body?: unknown;
}

function buildUrl(path: string, query?: FetchOptions["query"]): string {
  const base = apiUrl().replace(/\/+$/, "");
  let url = `${base}${path.startsWith("/") ? path : `/${path}`}`;
  if (query) {
    const search = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === "") continue;
      search.set(k, String(v));
    }
    const qs = search.toString();
    if (qs) url += `?${qs}`;
  }
  return url;
}

/**
 * Single-shot authenticated fetcher used by every admin API helper.
 *
 * Always sets `cache: "no-store"` — admin data must never be cached. Network
 * errors and non-2xx responses are caught and surfaced via `T | null` so
 * callers (Server Components) can render an inline error banner instead of
 * blowing up the route. JSON parse failures degrade the same way.
 */
async function adminFetch<T>(
  token: string,
  path: string,
  opts: FetchOptions = {},
): Promise<T | null> {
  try {
    const url = buildUrl(path, opts.query);
    const init: RequestInit = {
      method: opts.method ?? "GET",
      cache: "no-store",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
    };
    if (opts.body !== undefined) {
      init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(url, init);
    if (!res.ok) {
      // 403 from /me is a normal "not an admin" signal — return null and let
      // the caller (the layout) redirect appropriately.
      return null;
    }
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Auth + system
// ---------------------------------------------------------------------------

export async function getAdminMe(token: string): Promise<AdminMe | null> {
  return adminFetch<AdminMe>(token, "/admin/me");
}

export async function getSystemHealth(token: string): Promise<SystemHealth | null> {
  return adminFetch<SystemHealth>(token, "/admin/system/health");
}

export interface ActionsParams {
  target_user_id?: string;
  admin_user_id?: string;
  limit?: number;
  cursor?: string;
}

export async function getActions(
  token: string,
  params: ActionsParams = {},
): Promise<ActionsPage> {
  const result = await adminFetch<ActionsPage>(token, "/admin/actions", {
    query: {
      target_user_id: params.target_user_id,
      admin_user_id: params.admin_user_id,
      limit: params.limit,
      cursor: params.cursor,
    },
  });
  return result ?? { items: [], cursor: null };
}

// ---------------------------------------------------------------------------
// Users + per-user reads
// ---------------------------------------------------------------------------

export async function listUsers(
  token: string,
  q?: string,
  cursor?: string,
  limit?: number,
): Promise<UsersPage> {
  const result = await adminFetch<UsersPage>(token, "/admin/users", {
    query: { q, cursor, limit },
  });
  return result ?? { users: [], cursor: null, stubbed: false };
}

export async function getOverview(
  token: string,
  userId: string,
): Promise<UserOverview | null> {
  return adminFetch<UserOverview>(token, `/admin/users/${encodeURIComponent(userId)}/overview`);
}

export async function listAgents(
  token: string,
  userId: string,
  cursor?: string,
  limit?: number,
): Promise<AgentsPage> {
  const result = await adminFetch<AgentsPage>(
    token,
    `/admin/users/${encodeURIComponent(userId)}/agents`,
    { query: { cursor, limit } },
  );
  return result ?? { agents: [], cursor: null, container_status: "unknown" };
}

export async function getAgentDetail(
  token: string,
  userId: string,
  agentId: string,
): Promise<AgentDetail | null> {
  return adminFetch<AgentDetail>(
    token,
    `/admin/users/${encodeURIComponent(userId)}/agents/${encodeURIComponent(agentId)}`,
  );
}

export async function getPosthog(
  token: string,
  userId: string,
  limit?: number,
): Promise<PosthogTimeline> {
  const result = await adminFetch<PosthogTimeline>(
    token,
    `/admin/users/${encodeURIComponent(userId)}/posthog`,
    { query: { limit } },
  );
  return result ?? { events: [], stubbed: false, missing: true, error: null };
}

export interface LogsOptions {
  level?: string;
  hours?: number;
  limit?: number;
  cursor?: string;
}

export async function getLogs(
  token: string,
  userId: string,
  opts: LogsOptions = {},
): Promise<LogsPage> {
  const result = await adminFetch<LogsPage>(
    token,
    `/admin/users/${encodeURIComponent(userId)}/logs`,
    {
      query: {
        level: opts.level,
        hours: opts.hours,
        limit: opts.limit,
        cursor: opts.cursor,
      },
    },
  );
  return result ?? { events: [], cursor: null, missing: true, error: null };
}

export async function getCloudwatchUrl(
  token: string,
  userId: string,
  start: string,
  end: string,
  level: string,
): Promise<CloudwatchUrlResponse | null> {
  return adminFetch<CloudwatchUrlResponse>(
    token,
    `/admin/users/${encodeURIComponent(userId)}/cloudwatch-url`,
    { query: { start, end, level } },
  );
}

// ---------------------------------------------------------------------------
// Container — available images (queue-image-update dropdown)
// ---------------------------------------------------------------------------

export interface AvailableImage {
  image_uri: string;
  tag: string;
  digest?: string | null;
  pushed_at?: string | null;
}

export interface AvailableImagesResponse {
  images: AvailableImage[];
}

export async function listAvailableImages(
  token: string,
  limit?: number,
): Promise<AvailableImagesResponse> {
  const result = await adminFetch<AvailableImagesResponse>(
    token,
    "/admin/container/available-images",
    { query: { limit } },
  );
  return result ?? { images: [] };
}

