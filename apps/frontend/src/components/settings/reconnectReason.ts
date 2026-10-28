import type { Connection } from "@/hooks/useConnections";

/** Our own reason codes: the grant failed on our side, not the provider's. */
const LOCAL_REASONS = new Set([
  "no_access_token",
  "bad_expiry",
  "no_refresh_token",
  "refresh_error",
  "unspecified",
]);

/** One plain line under Reconnect saying why, or null when there is nothing to say. */
export function reconnectReasonLine(c: Connection): string | null {
  if (c.status !== "reconnect_required" || !c.reconnect_reason) return null;
  const code = c.reconnect_reason.split(":")[0];
  if (code === "lightbulb_retired_grant") {
    return "Lightbulb moved this connection to a new sign-in. Reconnect to continue.";
  }
  return LOCAL_REASONS.has(code)
    ? `The saved sign-in can't be renewed (${code}). Reconnect to continue.`
    : `${c.display_name} ended the sign-in (${code}). Reconnect to continue.`;
}
