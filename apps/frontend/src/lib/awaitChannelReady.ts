// Deferral-aware readiness poll for channel (Telegram/Slack/Discord) setup.
//
// Why this exists: OpenClaw 6.6 defers a channel reload until in-flight work
// drains (default ceiling 300s), and on a busy container the per-account
// `lastStartAt` may not advance for minutes. The old wizard gave up at a hard
// 60s and DELETEd the just-configured (valid) bot, blaming the token. This
// poller instead waits past the deferral ceiling, surfaces the wait, and keys
// success on a real `connected` transition rather than the bare `running:true`
// the gateway sets BEFORE auth runs (server-channels.ts:584-591) — so a bad
// token surfaces as a real `lastError` (fail-fast) instead of a false success.

export type ChannelAccountSnapshot = {
  accountId?: string;
  running?: boolean;
  connected?: boolean;
  lastError?: string | null;
  lastStartAt?: number | null;
};

type ChannelStatus = {
  channelAccounts?: Record<string, ChannelAccountSnapshot[]>;
};

export type ChannelReadyOutcome =
  | { status: "ready" }
  | { status: "error"; lastError: string }
  | { status: "timeout" }
  | { status: "cancelled" };

export type AwaitChannelReadyOptions = {
  /** Gateway RPC caller (the wizard passes useGatewayRpcMutation's callRpc). */
  callRpc: (method: string, params?: Record<string, unknown>) => Promise<unknown>;
  provider: string;
  accountId: string;
  /** `lastStartAt` captured BEFORE the config PATCH, so we only trust the
   * snapshot once the account has actually restarted (advanced past this). */
  initialStartAt: number | null;
  /** Progress note sink so the UI can show "applying / restarting" instead of
   * a frozen spinner. */
  onNote?: (note: string) => void;
  /** Cooperative cancel (e.g. a Cancel button). Checked each poll. */
  shouldCancel?: () => boolean;
  // Injectable for deterministic tests:
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  /** Overall ceiling. Default 390s = OpenClaw's 300s deferral ceiling + ~90s for
   * chokidar EFS detection, restart, and auth on a busy 0.5-vCPU box. Timeout is
   * non-destructive (the account is kept), so an over-short ceiling never deletes
   * a valid bot — but too short would show the "taking longer" message early. */
  deadlineMs?: number;
  pollMs?: number;
};

export const APPLYING_NOTE =
  "Applying your settings — this can take a few minutes if your agent is busy.";
export const STARTING_NOTE = "Starting your bot…";
export const RESTARTING_NOTE = "Your container is restarting to apply the change…";

export async function awaitChannelReady(
  opts: AwaitChannelReadyOptions,
): Promise<ChannelReadyOutcome> {
  const {
    callRpc,
    provider,
    accountId,
    initialStartAt,
    onNote,
    shouldCancel,
    now = () => Date.now(),
    sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms)),
    deadlineMs = 390_000,
    pollMs = 2_000,
  } = opts;

  const deadline = now() + deadlineMs;
  let sawRestart = false;

  while (now() < deadline) {
    if (shouldCancel?.()) return { status: "cancelled" };
    try {
      const status = (await callRpc("channels.status", { probe: false })) as ChannelStatus;
      const accounts = status?.channelAccounts?.[provider] ?? [];
      const acct = accounts.find((a) => a.accountId === accountId);
      const newStartAt = acct?.lastStartAt ?? null;
      const restarted =
        newStartAt != null && (initialStartAt == null || newStartAt > initialStartAt);
      if (restarted) {
        sawRestart = true;
        // `connected` is the post-auth readiness signal all three channel
        // plugins set (telegram/slack/discord). Bare `running:true` is set
        // before auth, so it is NOT trusted as success.
        if (acct?.connected) return { status: "ready" };
        if (acct?.lastError) return { status: "error", lastError: acct.lastError };
      }
      onNote?.(sawRestart ? STARTING_NOTE : APPLYING_NOTE);
    } catch {
      // channels.status throws while the gateway is mid-restart; expected
      // during a deferred reload. Keep waiting.
      onNote?.(RESTARTING_NOTE);
    }
    if (shouldCancel?.()) return { status: "cancelled" };
    await sleep(pollMs);
  }
  return { status: "timeout" };
}
