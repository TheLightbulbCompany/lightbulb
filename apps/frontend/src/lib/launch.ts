import { BACKEND_URL } from "@/lib/api";

export type LaunchPhase = "waitlist" | "activation" | "live" | "ended";

export interface LaunchInfo {
  phase: LaunchPhase;
  drop_at: string;
  competition_start_at: string;
  competition_end_at: string;
  signups: number;
  competing: number;
  on_board: number;
  pot_cents: number;
  comp_slots: number;
  pot_per_entrant_cents: number;
  /** Not sent by `GET /launch` today — the public info route predates the
   * queue-jump setting. Optional so `LaunchPage`'s rules copy can render the
   * numeric sentence once the backend adds it, and the generic fallback
   * copy until then. */
  referral_jump?: number;
}

export interface LeaderboardRow {
  rank: number;
  display_name: string;
  delta_cents: number;
  pct: number | null;
  currency: string;
}

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

export interface LaunchWaitlist {
  position: number;
  /** `max(1, position - referral_jump * referrals_signed_up)` — the queue
   * position after referral jumps; the number every UI surface shows. */
  effective_position: number;
  /** `position - effective_position` — how many spots referrals moved you up. */
  moved_up: number;
  /** Spots per referral, echoed for the "each referral moves you up N spots" copy. */
  referral_jump: number;
  referral_code: string;
  in_comp_window: boolean;
  referral_link: string;
  referrals_signed_up: number;
  referrals_paid: number;
  free_months_banked: number;
}

export interface LaunchMe {
  phase: LaunchPhase;
  waitlist: LaunchWaitlist | null;
  activated: boolean;
  activated_at: string | null;
  entry: LaunchEntry | null;
  joined: boolean;
  has_key: boolean;
  can_join: boolean;
  reason: string | null;
  idea: string | null;
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
