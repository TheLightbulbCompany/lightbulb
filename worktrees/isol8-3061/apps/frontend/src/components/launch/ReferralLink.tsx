"use client";

import * as React from "react";
import { capture } from "@/lib/analytics";
import { SITE_URL } from "@/lib/domain";

export function ReferralLink({ code, link }: { code: string; link?: string }) {
  // Fallback only — every caller today passes `link` (the backend's own
  // `referral_link`, which points at the same root). `?ref=` on the root is
  // captured by LaunchRefCapture (mounted on `/`) into localStorage, and
  // `useReferralAttribution` POSTs it to `/launch/referral` once the visitor
  // has an account — so the root is the right destination either way.
  const href = link ?? `${SITE_URL}/?ref=${code}`;
  const [copied, setCopied] = React.useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(href);
      setCopied(true);
      capture("launch_referral_copied", {});
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — the input is selectable */
    }
  };
  return (
    <div className="lc-field">
      <span>Share your link — every referral who subscribes moves you up.</span>
      <div className="lc-row">
        <input readOnly data-mono value={href} aria-label="Your referral link" onFocus={(e) => e.currentTarget.select()} />
        <button type="button" className="ld-btn ld-btn--ghost" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
      </div>
    </div>
  );
}
