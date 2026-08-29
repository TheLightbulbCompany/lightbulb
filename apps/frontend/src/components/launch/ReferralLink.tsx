"use client";

import * as React from "react";
import { capture } from "@/lib/analytics";
import { SITE_URL } from "@/lib/domain";

export function ReferralLink({ code, link }: { code: string; link?: string }) {
  // Fallback only — every caller today passes `link` (the backend's own
  // `referral_link`). `?ref=` on the root is what `useLaunchRef` picks up and
  // `POST /launch/referral` attributes on a post-drop sign-up, so the root is
  // still the right fallback destination even with sign-up as the waitlist.
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
      <span>Refer a friend who subscribes → you get a month free.</span>
      <div className="lc-row">
        <input readOnly data-mono value={href} aria-label="Your referral link" onFocus={(e) => e.currentTarget.select()} />
        <button type="button" className="ld-btn ld-btn--ghost" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
      </div>
    </div>
  );
}
