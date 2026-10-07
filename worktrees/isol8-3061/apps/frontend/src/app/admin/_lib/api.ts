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
  /** The workspace's AgentCore runtime ARN; null while unprovisioned. */
  runtime_arn: string | null;
  /** The row's own status, unmapped. "unknown" when there is no runtime yet. */
  status: string;
}

export interface FleetData {
  counts: Record<string, number> | null;
  grid: FleetGridItem[] | null;
}

/** One ISO week of activity, from the messaging table (audit §6 metric 2).
 * Every field is `null` when the messaging scan failed — the page renders that
 * as an em dash / a gap, never as a zero. */
export interface GrowthWeeklyActive {
  week: string;
  workspaces: number | null;
  multiplayer: number | null;
  multiplayer_share: number | null;
}
/** One signup-week cohort: how many became measurable, activated, and pay (§6 metrics 1 + 4).
 * `signups`/`paying` are billing-fed and survive a failed messaging scan; the
 * other two go `null` with it. */
export interface GrowthCohort {
  week: string;
  signups: number;
  /** Cohort members whose 7-day activation window has closed (or that already activated). */
  measurable: number | null;
  activated: number | null;
  paying: number;
}
export interface GrowthSegment {
  workspaces: {
    total: number;
    paying: number;
    paying_by_plan: Record<string, number>;
    paying_by_provider: Record<string, number>;
    trial_active: number;
    trial_expired: number;
    signups_by_week: { week: string; n: number }[];
  };
  /** Messaging-fed; every field is `null` when `sources.messaging` is not "ok". */
  activation: { activated: number | null; measurable: number | null; rate: number | null };
  weekly_active: GrowthWeeklyActive[];
  answered: {
    dispatched: number | null;
    answered: number | null;
    rate: number | null;
    p50_reply_s: number | null;
  };
  cohorts: GrowthCohort[];
  retention_w4: { eligible: number | null; retained: number | null; rate: number | null };
  messaging: { rooms: number | null; human_messages_this_week: number | null };
  /** Daily-resolution series for the charts — 56 rows each, zeros included.
   * `human_messages` and `activated` carry `null` when the messaging scan failed. */
  daily: {
    signups: { day: string; n: number }[];
    human_messages: { day: string; n: number | null }[];
    activated: { day: string; n: number | null }[];
  };
}
export interface GrowthData {
  generated_at: string;
  window_weeks: number;
  weeks: string[];
  /** 56 ISO dates, oldest first. */
  days: string[];
  segments: { external: GrowthSegment; internal: GrowthSegment };
  internal_owner_ids: string[];
  gate_blocked_7d: { code: string; n: number }[];
  /** Sparse — one row per (day, code) that occurred. */
  gate_blocked_daily: { day: string; code: string; n: number }[];
  /** openclaw-work-dlq.fifo depth; null when the CloudWatch read failed. */
  dlq_depth: number | null;
  posthog: {
    funnel: { week: string; event: string; owners: number }[];
    signup_sources: { source: string; owners: number }[];
    /** Sparse — one row per (day, event) that occurred. */
    funnel_daily: { day: string; event: string; owners: number }[];
  };
  sources: Record<"ddb" | "messaging" | "posthog" | "cloudwatch" | "clerk", string>;
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
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
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
    provision_generation?: string | null;
    onboarded_at?: string | null;
    stop_reason?: string | null;
    agentcore_runtime_arn?: string | null;
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
    starter_agent_ids?: Record<string, string> | null;
    scheduled_purge_at?: string | null;
    onboarding_generation?: string | null;
    funnel_state?: string | null;
    funnel_submitted_at?: string | null;
    funnel_completed_at?: string | null;
    onboarding_goal_id?: string | null;
    onboarding_project_id?: string | null;
    onboarding_submitted_by?: string | null;
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

export async function getGrowth(token: string): Promise<OpsEnvelope<GrowthData> | null> {
  return adminFetch<OpsEnvelope<GrowthData>>(token, "/ops/growth");
}

export interface LaunchOpsEntry {
  owner_id: string;
  /** A live Stripe subscription exists for this owner (billing row check). */
  has_subscription: boolean;
  display_name: string | null;
  public: boolean;
  waitlist_position: number | null;
  currency: string | null;
  baseline_cents: number;
  window_cents: number;
  delta_cents: number;
  pct: number | null;
  refreshed_at: string | null;
  refresh_error: string | null;
}

/** A JOINED competition-sheet row (`ops/launch.py` filters on `joined_at`),
 * sorted (effective_position, position). */
export interface LaunchSheetRow {
  email: string;
  position: number;
  /** `position` after referral queue-jumps — the sheet/CSV sort key. */
  effective_position: number;
  display_name: string | null;
  public: boolean;
  referral_link: string;
  created_at: string | null;
  joined_at: string | null;
  user_id: string | null;
  referred_by: string | null;
  referrals_signed_up: number;
  referrals_subscribed: number;
  winner: boolean;
  /** Pre-close always true (nobody ruled out yet); post-close the stored flag. */
  compete_eligible: boolean;
  coupon_attached_at: string | null;
  winner_email_sent_at: string | null;
}

/** One team submission (`POST /launch/team-interest`) — the founders' manual
 * lane. Teams never join the sheet, so a team entry has no position and no
 * coupon; `counted` is the founders' verdict, and ONLY a counted entry
 * contributes `contribution_cents` to the pot. */
export interface LaunchTeamEntry {
  owner_id: string;
  team_name: string;
  seats: number;
  note: string;
  submitter_email: string;
  submitter_user_id: string;
  submitter_org_slug: string;
  counted: boolean;
  submitted_at: string | null;
  /** Last write of any kind. `submitted_at` survives a resubmission, so this
   * is the only thing that moves when a submitter edits after review. */
  updated_at: string | null;
  /** What this entry adds to the pot once counted — per-entrant + per-seat × seats. */
  contribution_cents: number;
}

/** The launch-settings row's admin projection (`to_admin` — everything but
 * `mint_nonce`). The pot fields are INTERNAL — they appear here and nowhere
 * public. `closed_at`/`final_*` are stamped by the close action, never PUT. */
export interface LaunchSettings {
  enabled: boolean;
  sheet_closes_at: string | null;
  competition_start_at: string | null;
  competition_end_at: string | null;
  comp_slots: number;
  pot_front_cents: number;
  pot_per_entrant_cents: number;
  /** Per SEAT of a counted team entry, on top of the per-entrant amount. */
  pot_per_seat_cents: number;
  /** Queue-jump: sheet spots per counted (subscribed) referral. */
  referral_jump: number;
  stripe_coupon_id: string;
  closed_at: string | null;
  final_pot_cents: number | null;
  final_roster_count: number | null;
  updated_at?: string;
  updated_by?: string;
}

/** The nine admin-editable fields — the exact `PUT /ops/launch/settings`
 * body (full replace; every field required, dates nullable). */
export type LaunchSettingsInput = Pick<
  LaunchSettings,
  | "enabled"
  | "sheet_closes_at"
  | "competition_start_at"
  | "competition_end_at"
  | "comp_slots"
  | "pot_front_cents"
  | "pot_per_entrant_cents"
  | "pot_per_seat_cents"
  | "referral_jump"
>;

/** POST /ops/launch/close result — the per-step counts of the close run. */
/** The finished close run's summary, persisted on the settings row by the
 * background task (POST /ops/launch/close is a 202 starter). All fields are
 * optional: a failed run writes only `error`. */
export interface LaunchCloseResult {
  error?: string;
  closed_at?: string | null;
  sheet_count?: number;
  winners?: number;
  paying?: number;
  final_roster_count?: number | null;
  final_pot_cents?: number | null;
  coupons_attached?: number;
  coupon_failures?: number;
  /** Paying winners NOT on the Workspace plan — the product-restricted coupon
   * would discount $0, so they were skipped and got the unpaid email. */
  coupon_skipped_nonflat?: number;
  /** Total close emails sent across all three cohorts. */
  emails_sent?: number;
  emails_winner?: number;
  /** Paying non-winners — still competing, told so. */
  emails_competing?: number;
  /** Non-winners who don't pay — out of the competition, told so. */
  emails_not_selected?: number;
  email_failures?: number;
}

/** The full sheet payload — only when the competition is enabled and the
 * read computed cleanly (`enabled: true`, `source_state: "ok"`). */
export interface LaunchOpsSheet {
  generated_at: string;
  settings: LaunchSettings;
  enabled: true;
  phase: "open" | "closed" | "competition" | "ended";
  schedule: { sheet_closes_at: string; competition_start_at: string; competition_end_at: string; comp_slots: number };
  signups: number;
  sheet: { total: number; joined: number; winners: number };
  competing: number;
  on_board: number;
  // The pot — ADMIN-ONLY, never in any public payload.
  pot_front_cents: number;
  pot_per_entrant_cents: number;
  pot_per_seat_cents: number;
  projected_pot_cents: number;
  closed: boolean;
  closed_at: string | null;
  /** A close background task started and has not written its result yet. */
  close_running: boolean;
  close_result: LaunchCloseResult | null;
  final_pot_cents: number | null;
  final_roster_count: number | null;
  referrals: { signed_up: number; subscribed: number };
  /** Team submissions: how many, how many the founders marked counted, and
   * what those counted ones add to the pot. */
  teams: { total: number; counted: number; contribution_cents: number };
  team_entries: LaunchTeamEntry[];
  entries: LaunchOpsEntry[];
  sheet_rows: LaunchSheetRow[];
}

/** The disabled/unset shape (`core.services.ops.launch.compute`'s early-return
 * branch) — the settings row is still readable so the admin console can turn
 * the feature on, but there is nothing to compute a sheet from. */
export interface LaunchOpsDisabled {
  settings: LaunchSettings;
  enabled: false;
}

export type LaunchOpsData = LaunchOpsSheet | LaunchOpsDisabled;

export async function getLaunchOps(token: string): Promise<OpsEnvelope<LaunchOpsData> | null> {
  return adminFetch<OpsEnvelope<LaunchOpsData>>(token, "/ops/launch");
}

/** One launch email's editable copy. `defaults` is the copy shipped in the
 * backend module (the source of truth); `override` holds only the fields an
 * admin has actually replaced — a blank field falls back to the default, so
 * clearing all three IS "reset to default". `preview_html` renders the
 * EFFECTIVE body with sample values. */
export interface LaunchEmailVariant {
  variant: string;
  placeholders: string[];
  defaults: { subject: string; lead: string; detail: string };
  override: { subject?: string; lead?: string; detail?: string };
  preview_subject: string;
  preview_html: string;
}

export async function getLaunchEmailCopy(token: string): Promise<{ variants: LaunchEmailVariant[] } | null> {
  return adminFetch<{ variants: LaunchEmailVariant[] }>(token, "/ops/launch/email-copy");
}
