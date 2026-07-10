"use client";

import { useCallback, useState } from "react";

export interface TeamBillingCardProps {
  seatCount: number | null;
  seatPriceUsd: number | null;
  ownerRole: "admin" | "member";
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
 */
export function TeamBillingCard({
  seatCount,
  seatPriceUsd,
  ownerRole,
  isSubscribed = false,
  onSubscribe,
  onManage,
}: TeamBillingCardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const seats = seatCount ?? 0;
  const price = seatPriceUsd ?? 0;
  const total = seats * price;
  const isAdmin = ownerRole === "admin";

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
    <div className="settings-card" style={{ maxWidth: 440 }}>
      <div className="settings-card-title">Teams billing</div>
      <div className="settings-card-desc" style={{ marginTop: 4 }}>
        {seats} {seats === 1 ? "seat" : "seats"} × ${price} = <strong>${total}/mo</strong>. Every member of your
        organization is a seat.
      </div>

      {error && (
        <div className="settings-alert settings-alert-error" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}

      <div style={{ marginTop: 16 }}>
        {!isAdmin ? (
          <p className="settings-card-desc">Ask your org admin to set up Teams billing.</p>
        ) : isSubscribed ? (
          <button type="button" className="settings-btn-outline" disabled={busy} onClick={() => run(onManage)}>
            Manage billing
          </button>
        ) : (
          <button type="button" className="settings-btn-save" disabled={busy} onClick={() => run(onSubscribe)}>
            {busy ? "Starting checkout…" : "Subscribe your team"}
          </button>
        )}
      </div>
    </div>
  );
}
