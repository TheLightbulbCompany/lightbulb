import "server-only";

import { randomUUID } from "crypto";

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
// Admin diagnostics envelope (mirrors the backend Ops DTO in routers/ops.py)
// ---------------------------------------------------------------------------

export interface OpsEnvelope<T> {
  data: T;
  freshness_ts: string;
  source_state: "ok" | "degraded" | "stale" | "unavailable" | "partial";
  errors: { domain: string; reason: string }[];
}

export interface FleetGridItem {
  owner_id: string;
  /** Best-effort human label (user email / org name); null when unresolved. */
  display_name?: string | null;
  service_name: string;
  status: string;
  desired_count: number;
  running_count: number;
  pending_count: number;
  rollout_state: string | null;
}

export interface FleetData {
  counts: Record<string, number> | null;
  grid: FleetGridItem[] | null;
}

export interface DeployRow {
  stack_name: string;
  status: string;
  status_reason: string | null;
  last_updated: string | null;
  root_cause: string | null;
}

export interface AlarmRow {
  name: string;
  metric: string | null;
  description?: string | null;
  state_reason?: string | null;
  severity?: "critical" | "warning" | "unknown";
  /** CloudWatch console deep link to the alarm. */
  console_url?: string | null;
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

interface AdminHttpArgs {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  path: string;
  token: string;
  query?: FetchOptions["query"];
  body?: unknown;
  /** When true, attach a per-request `Idempotency-Key` (mutations). */
  idempotent?: boolean;
}

interface AdminHttpResult {
  ok: boolean;
  status: number;
  data: unknown;
}

/**
 * Single request core shared by the read path (`adminFetch`) and the
 * Server-Action mutation path (`_actions/_shared.ts adminRequest`).
 *
 * Builds the URL, sets `Authorization` / `Content-Type` / optional
 * `Idempotency-Key`, always uses `cache: "no-store"` (admin data must never be
 * cached), and returns `{ ok, status, data }`. The body is read as text then
 * JSON-parsed, falling back to the raw string, so empty and non-JSON responses
 * degrade instead of throwing. Network failures surface as
 * `{ ok: false, status: 0, data: undefined }`.
 */
export async function adminHttp({
  method,
  path,
  token,
  query,
  body,
  idempotent,
}: AdminHttpArgs): Promise<AdminHttpResult> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
  if (idempotent) {
    // Dedupe double-clicks server-side on mutation paths.
    headers["Idempotency-Key"] = randomUUID();
  }
  const init: RequestInit = {
    method,
    cache: "no-store",
    headers,
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  try {
    const res = await fetch(buildUrl(path, query), init);
    const text = await res.text();
    let data: unknown = undefined;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
    return { ok: res.ok, status: res.status, data };
  } catch {
    return { ok: false, status: 0, data: undefined };
  }
}

/**
 * Single-shot authenticated fetcher used by every admin API read helper.
 *
 * Thin wrapper over {@link adminHttp}: network errors and non-2xx responses are
 * surfaced as `null` so callers (Server Components) can render an inline error
 * banner instead of blowing up the route. A 403 from `/me` is the normal
 * "not an admin" signal and degrades the same way. Empty / unparseable bodies
 * also degrade to `null`.
 */
async function adminFetch<T>(
  token: string,
  path: string,
  opts: FetchOptions = {},
): Promise<T | null> {
  const { ok, data } = await adminHttp({
    method: opts.method ?? "GET",
    path,
    token,
    query: opts.query,
    body: opts.body,
  });
  return ok ? ((data as T) ?? null) : null;
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

export interface AdminProvisioning {
  owner_id: string;
  org: unknown;
  status_contract:
    | ({
        runtime_phase?: string;
        lifecycle?: string;
        chat_available?: boolean;
        teams_state?: string | null;
      } & Record<string, unknown>)
    | null;
  container: {
    status?: string;
    substatus?: string | null;
    last_error?: string | null;
    last_error_at?: string | null;
    retryable?: boolean | null;
    claim_stale?: boolean;
    provision_generation?: string | null;
    provision_claim_id?: string | null;
    provision_claimed_at?: number | null;
    provision_attempt_token?: string | null;
    provision_execution_name?: string | null;
    onboarded_at?: string | null;
    bedrock_revoked?: boolean | null;
    stop_reason?: string | null;
    service_name?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
    /**
     * Present INSTEAD of the fields above when the backend's containers-row
     * read itself failed (DDB timeout) — distinguishable from `container ===
     * null` (no row at all). Mirrors the `error`/`source` idiom already used
     * on `AgentDetail`/`LogsPage`/etc. in this file.
     */
    error?: string;
    source?: string;
  } | null;
  paperclip: {
    status?: string | null;
    company_id?: string | null;
    org_id?: string | null;
    paperclip_user_id?: string | null;
    last_error?: string | null;
    seeded_agent_ids?: string[] | null;
    scheduled_purge_at?: string | null;
    onboarding_generation?: string | null;
    funnel_state?: string | null;
    funnel_submitted_at?: string | null;
    funnel_completed_at?: string | null;
    onboarding_goal_id?: string | null;
    onboarding_project_id?: string | null;
    onboarding_submitted_by?: string | null;
    bootstrap_state?: string | null;
    bootstrap_attempts?: number | null;
    bootstrap_last_error?: string | null;
    bootstrap_dispatch_id?: string | null;
    bootstrap_dispatched_at?: string | null;
    bootstrap_delivered_at?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
    /**
     * Present INSTEAD of the fields above when the backend's Paperclip-company
     * read itself failed (DDB timeout) — distinguishable from `paperclip ===
     * null` (no row at all). Mirrors the `container` read-failure idiom above.
     */
    error?: string;
    source?: string;
  } | null;
  gate: { code?: string; title?: string; message?: string } | null;
  sfn_execution:
    | {
        name?: string;
        status?: string;
        start_date?: string;
        stop_date?: string;
        /**
         * Present INSTEAD of the fields above when the backend's SFN
         * DescribeExecution read itself failed — distinguishable from
         * `sfn_execution === null` (no execution stamped at all). Mirrors the
         * `container` read-failure idiom above.
         */
        error?: string;
        source?: string;
      }
    | null;
}

export async function getProvisioning(
  token: string,
  userId: string,
): Promise<AdminProvisioning | null> {
  return adminFetch<AdminProvisioning>(
    token,
    `/admin/users/${encodeURIComponent(userId)}/provisioning`,
  );
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
  /** Mirrors openclaw-version.json#dev.tag at the time of the request. */
  current_dev_tag?: string;
  /** Mirrors openclaw-version.json#prod.tag at the time of the request. */
  current_prod_tag?: string;
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

// ---------------------------------------------------------------------------
// Admin diagnostic reads (fleet, deployments, and alarms)
// ---------------------------------------------------------------------------

export async function getFleet(token: string): Promise<OpsEnvelope<FleetData> | null> {
  return adminFetch<OpsEnvelope<FleetData>>(token, "/ops/fleet");
}

export async function getDeploys(token: string): Promise<OpsEnvelope<DeployRow[]> | null> {
  return adminFetch<OpsEnvelope<DeployRow[]>>(token, "/ops/deploys");
}

export async function getAlarms(token: string): Promise<OpsEnvelope<AlarmRow[]> | null> {
  return adminFetch<OpsEnvelope<AlarmRow[]>>(token, "/ops/alarms");
}
