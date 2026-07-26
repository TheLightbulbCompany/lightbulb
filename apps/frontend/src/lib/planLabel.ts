/**
 * The single `subscription_status` → display-label mapping.
 *
 * The raw enum must never reach a user surface: the plan badge is
 * `text-transform: uppercase`, so `trial_local` rendered as "TRIAL_LOCAL".
 * Settings already had this mapping inline; the two chat shells did not, which
 * is exactly the drift one shared helper prevents.
 *
 * Statuses other than the free tier pass through unchanged — callers apply
 * their own `capitalize` treatment, as Settings does.
 */
export function planLabel(
  status: string | null | undefined,
  opts?: { expired?: boolean },
): string | null {
  if (!status) return null;
  if (status === "trial_local") {
    return opts?.expired ? "Free tier ended" : "Free tier";
  }
  return status;
}
