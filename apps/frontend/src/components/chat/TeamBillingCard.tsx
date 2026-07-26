"use client";

import { useCallback, useState } from "react";

export interface TeamBillingCardProps {
  seatCount: number | null;
  seatPriceUsd: number | null;
  ownerRole: "admin" | "member";
  /**
   * Mirrors the provision gate's `blocked.action.admin_only`. Combined with
   * `ownerRole` this decides whether the CTA is shown at all — the same
   * predicate the generic blocked card in ProvisioningStepper uses. Defaults
   * to true because every Teams-billing action is admin-only today; passing
   * the wire field explicitly keeps the UI honest if that ever changes.
   */
  adminOnly?: boolean;
  isSubscribed?: boolean;
  onSubscribe?: () => Promise<void>;
  onManage?: () => Promise<void>;
}

/**
 * The one Teams-billing paywall card. Rendered in two places (the /chat
 * blocked screen and the settings billing panel), so it owns no data fetching
 * — seat count + price are passed in from the /billing/account payload, and
 * the actions are injected by the caller. Admin-gated: members see an
 * ask-your-admin message with no CTA.
 *
 * Styling note — this component is deliberately styled with Tailwind
 * utilities, NOT the `.settings-*` classes the rest of the settings panels
 * use. Those classes live in `src/app/settings/settings.css`, and a plain CSS
 * import in the App Router is scoped to the route segment that imports it. On
 * /chat that stylesheet is never loaded, so a `.settings-card` /
 * `.settings-btn-save` version of this card painted as raw unstyled text (a
 * bare <button> is reset to inherited font/color by Tailwind preflight, so the
 * CTA read as a paragraph). Anything mounted on more than one route has to
 * carry its own styles.
 */
export function TeamBillingCard({
  seatCount,
  seatPriceUsd,
  ownerRole,
  adminOnly = true,
  isSubscribed = false,
  onSubscribe,
  onManage,
}: TeamBillingCardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const price = seatPriceUsd ?? 0;
  const canAct = !(adminOnly && ownerRole !== "admin");
  // seatCount is null while Clerk membership is still loading (or
  // unavailable). Rendering "0 seats × $20 = $0/mo" in that window is a
  // misleading flash — show a neutral placeholder until it resolves to a
  // real number.
  const seatsKnown = seatCount != null;
  const seats = seatCount ?? 0;
  const total = seats * price;

  const run = useCallback(
    async (fn?: () => Promise<void>) => {
      if (!fn) return;
      setBusy(true);
      setError(null);
      try {
        await fn();
      } catch (err) {
        setBusy(false);
        setError(err instanceof Error ? err.message : "Something went wrong. Please try again.");
      }
    },
    [],
  );

  return (
    <div
      data-testid="team-billing-card"
      style={{ fontFamily: "var(--font-dm-sans), 'DM Sans', sans-serif" }}
      className="w-full max-w-[440px] rounded-2xl border border-[#e0dbd0] bg-white px-4 py-5 text-left shadow-[0_1px_2px_rgba(28,25,23,0.04)] sm:p-7"
    >
      <div className="text-base font-semibold leading-[1.4] text-[#1c1917]">Teams billing</div>
      <p className="mt-1 text-[13px] leading-[1.5] text-[#6b655e]">
        {seatsKnown ? (
          <>
            {seats} {seats === 1 ? "seat" : "seats"} × ${price} = <strong className="font-semibold text-[#1c1917]">${total}/mo</strong>. Every member of your
            organization is a seat.
          </>
        ) : (
          "Loading team size…"
        )}
      </p>

      {error && (
        <div
          role="alert"
          className="mt-3 rounded-[10px] border border-[rgba(182,48,37,0.2)] bg-[rgba(182,48,37,0.05)] px-3.5 py-2.5 text-[13px] leading-[1.5] text-[#b63025]"
        >
          {error}
        </div>
      )}

      <div className="mt-4">
        {!canAct ? (
          <p className="rounded-[10px] border border-[#e0dbd0] bg-[#faf7f2] px-3.5 py-3 text-[13px] leading-[1.5] text-[#6b655e]">
            Ask your org admin to set up Teams billing. Only an organization admin can start the team
            subscription.
          </p>
        ) : isSubscribed ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(onManage)}
            className="inline-flex min-h-11 items-center justify-center rounded-full border border-[rgba(6,64,43,0.2)] px-[18px] py-2.5 text-[13px] font-semibold text-[#06402B] transition-colors hover:border-[rgba(6,64,43,0.35)] hover:bg-[rgba(6,64,43,0.05)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            Manage billing
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(onSubscribe)}
            className="inline-flex min-h-11 items-center justify-center rounded-full bg-[#06402B] px-6 py-2.5 text-sm font-bold text-white transition-colors hover:bg-[#054d33] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "Starting checkout…" : "Subscribe your team"}
          </button>
        )}
      </div>
    </div>
  );
}
