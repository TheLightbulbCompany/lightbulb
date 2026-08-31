import { BACKEND_URL } from "@/lib/api";

export type LaunchPhase = "open" | "closed" | "competition" | "ended";

/** Public `GET /launch`. NO pot fields — the pot is admin-only and never
 * appears in any public payload. `signups` counts every sheet row including
 * attribution-only ones, so copy must say "signups", never "on the sheet". */
export interface LaunchInfo {
  phase: LaunchPhase;
  sheet_closes_at: string;
  competition_start_at: string;
  competition_end_at: string;
  signups: number;
  comp_slots: number;
  referral_jump: number;
}

export interface LeaderboardRow {
  rank: number;
  display_name: string;
  delta_cents: number;
  pct: number | null;
  currency: string;
}

/** The revenue-board entry dict returned by POST /launch/connect-stripe —
 * the only read surface for the caller's own board attachment. */
export interface LaunchEntry {
  display_name: string;
  public: boolean;
  entered_at: string | null;
  has_key: boolean;
  currency: string | null;
  baseline_cents: number;
  window_cents: number;
  delta_cents: number;
  refreshed_at: string | null;
  refresh_error: string | null;
}

/** The board-attachment projection carried on /launch/me for JOINED rows —
 * a subset of `LaunchEntry`, so a reload mid-competition shows the connected
 * state instead of inviting a key re-paste. */
export interface LaunchMeEntry {
  has_key: boolean;
  public: boolean;
  entered_at: string | null;
  delta_cents: number | null;
  refreshed_at: string | null;
  refresh_error: string | null;
  currency: string | null;
}

/** `GET /launch/me` (auth) — the caller's sheet status. `position` and the
 * referral fields are null until they explicitly joined; `compete_eligible`
 * is false when not joined and true for joined rows pre-close. */
export interface LaunchMe {
  phase: LaunchPhase;
  joined: boolean;
  position: number | null;
  /** `position` after referral queue-jumps — the number every surface shows. */
  effective_position: number | null;
  referrals_subscribed: number;
  referral_code: string | null;
  referral_link: string | null;
  credits_milestone_reached: 0 | 5 | 20;
  winner: boolean;
  compete_eligible: boolean;
  /** Friday's first run delivered — the join precondition. */
  onboarded: boolean;
  /** The board attachment; null until joined AND connected. */
  entry: LaunchMeEntry | null;
}

export const LAUNCH_REF_KEY = "launch_ref";

// Public reads use plain fetch: the shared client refuses to send without a
// Clerk token, and these routes are meant for signed-out visitors. No
// `cache: "no-store"` — these routes send Cache-Control themselves, and opting
// out of it here defeats both the browser cache and the backend's own TTL.
export async function fetchLaunchInfo(): Promise<LaunchInfo | null> {
  const res = await fetch(`${BACKEND_URL}/launch`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`launch_info_${res.status}`);
  return (await res.json()) as LaunchInfo;
}

export async function fetchLeaderboard(): Promise<{ competing: number; on_board: number; rows: LeaderboardRow[] }> {
  const res = await fetch(`${BACKEND_URL}/launch/leaderboard`);
  if (!res.ok) throw new Error(`leaderboard_${res.status}`);
  return (await res.json()) as { competing: number; on_board: number; rows: LeaderboardRow[] };
}

const SYMBOL: Record<string, string> = { usd: "$", eur: "€", gbp: "£", cad: "CA$", aud: "A$" };

export function formatMoney(cents: number, currency: string): string {
  const sym = SYMBOL[currency.toLowerCase()] ?? `${currency.toUpperCase()} `;
  const whole = Math.round(Math.abs(cents) / 100).toLocaleString("en-US");
  return `${cents < 0 ? "-" : ""}${sym}${whole}`;
}

export function formatPct(pct: number | null): string {
  if (pct === null) return "new";
  const n = Math.round(pct * 100);
  return n < 0 ? `−${Math.abs(n)}%` : `+${n}%`;
}
