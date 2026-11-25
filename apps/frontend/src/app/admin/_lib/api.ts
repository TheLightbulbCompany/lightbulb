import "server-only";

import { apiUrl } from "./url";

// Response shapes mirror apps/backend/routers/admin.py (GET /admin/me) and routers/ops.py (GET /ops/spark).

export interface AdminMe {
  user_id: string;
  email: string | null;
  is_admin: true;
}

export interface OpsEnvelope<T> {
  data: T;
  freshness_ts: string;
  source_state: "ok" | "degraded" | "stale" | "unavailable" | "partial";
  errors: { domain: string; reason: string }[];
}

/**
 * Authenticated GET. Network errors, non-2xx responses and empty bodies all
 * surface as `null` so Server Components can render an inline error banner
 * instead of blowing up the route (a 403 from `/admin/me` is the normal
 * "not an admin" signal). `no-store`: admin data must never be cached.
 */
async function adminFetch<T>(token: string, path: string): Promise<T | null> {
  try {
    const res = await fetch(`${apiUrl().replace(/\/+$/, "")}${path}`, {
      method: "GET",
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    });
    const text = await res.text();
    let data: unknown = undefined;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
    return res.ok ? ((data as T) ?? null) : null;
  } catch {
    return null;
  }
}

export async function getAdminMe(token: string): Promise<AdminMe | null> {
  return adminFetch<AdminMe>(token, "/admin/me");
}

/** k of n; n = 0 renders as a dash. */
export interface Kn {
  k: number;
  n: number;
}
export interface SparkPerson {
  installed: string;
  platform: string;
  version: string;
  provider: string;
  channel: string;
  furthest: string;
  last_task: string | null;
  email: string | null;
}

export interface SparkData {
  generated_at: string;
  wad: { last7: number; prev7: number; weekly: { week: string; partial: boolean; doers: number }[] };
  cohorts: {
    week: string;
    partial: boolean;
    platform: string;
    installs: number;
    settled: number;
    activated_24h: Kn;
    activated_d7: Kn;
    /** null = too early. */
    w4: Kn | null;
  }[];
  success: {
    weekly: { week: string; partial: boolean; platform: string; success: Kn }[];
    by_version: { key: string; success: Kn }[];
    by_provider: { key: string; success: Kn }[];
    failed_reasons: { key: string; count: number }[];
    problem_kinds: { key: string; count: number }[];
  };
  brain_unavailable_installs_24h: number;
  funnel: { step: string; k: number; n: number }[];
  /** installs query hit its row cap; older installs are missing. */
  truncated: boolean;
  downloads: { key: string; count: number }[];
  channels: { channel: string; activated_24h: Kn }[];
  people: SparkPerson[];
  money: {
    price_usd: number;
    /** null = no subscription_updated seen yet. */
    subs: {
      paying: number;
      mrr_usd: number;
      unknown: number;
      trial_to_paid: { month: string; k: number; n: number; partial: boolean }[];
      cancellations: { month: string; count: number; reasons: SparkCount[]; feedback: SparkCount[] }[];
    } | null;
    /** null = no voice_session_ended seen yet. */
    voice: {
      rate_usd_per_min: number;
      months: { month: string; minutes: number; users: number; p50: number; p90: number; cost_usd: number }[];
      top: { email: string | null; status: string; minutes: number; cost_usd: number; margin_usd: number }[];
    } | null;
  };
  time_to_first_task: { bucket: string; k: number; n: number }[];
  /** null = nobody has answered "how did you hear" yet. */
  heard_from: { choice: string; installs: number; activated_24h: Kn }[] | null;
  /** null = no onboarding_step seen yet. */
  setup: {
    steps: { step: string; shown: number; completed: Kn; skipped: Kn }[];
    signin_failures: SparkCount[];
    permissions: { permission: string; prompted: number; denied: Kn }[];
    unfinished: Kn | null;
  } | null;
  /** Last 30 days. Each part is null until its events were seen. */
  signals: {
    /** k = task_stopped, n = human task_started; how = the split by pressed / said / other. */
    stopped: { k: number; n: number; how: SparkCount[] } | null;
    /** ok = the actions were started (not that the work finished). people_30d is a lower bound. */
    automations: { runs: number; ok: Kn; with_brain: Kn; people_30d: number } | null;
    /** people_7d null = no 7-day figure (full access). */
    features: { feature: string; people_30d: number; people_7d: number | null }[] | null;
    /** k = people with a crash in 7 days, n = people who opened the app in 7 days (about). */
    crash: Kn | null;
    versions: { version: string; people_7d: number }[] | null;
    updates: { to_version: string; installs_30d: number }[] | null;
  };
}

export type SparkCount = { key: string; count: number };

export async function getSpark(token: string): Promise<OpsEnvelope<SparkData | null> | null> {
  return adminFetch<OpsEnvelope<SparkData | null>>(token, "/ops/spark");
}
