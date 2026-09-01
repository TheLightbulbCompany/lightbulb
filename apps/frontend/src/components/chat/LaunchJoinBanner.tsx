"use client";

import * as React from "react";
import useSWR from "swr";
import { useUser } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { useLaunchInfo } from "@/components/launch/useLaunchInfo";
import { joinErrorCopy } from "@/components/launch/JoinForm";
import { useReferralAttribution } from "@/components/launch/useReferralAttribution";
import type { LaunchMe } from "@/lib/launch";

/**
 * The in-product competition-sheet join surface: a slim banner in the
 * workspace shell (next to TrialBanner/OutOfCreditsBanner), shown while the
 * sheet is open to an onboarded owner who hasn't joined. Join POSTs inline —
 * no navigation — with an EMPTY display-name field (POST /launch/join
 * REQUIRES display_name). Deliberately no prefill: the board ranks
 * businesses, not people, and the owner's Paperclip company name isn't
 * reachable here without a brand-new cross-service request (see the
 * `name` state below), so a wrong default (the Clerk first name) would be
 * worse than none — the placeholder asks the right question instead. After
 * joining it shows the position + referral link, and only then becomes
 * dismissible (per-user localStorage key): the join nudge itself stays
 * until they join, /launch has the full rules.
 */

function dismissKey(userId: string): string {
  return `launch_sheet_banner_dismissed:${userId}`;
}

export function LaunchJoinBanner() {
  const api = useApi();
  const { user } = useUser();
  const info = useLaunchInfo();
  const open = info?.phase === "open";
  const { data: me, mutate } = useSWR<LaunchMe>(open ? "/launch/me" : null, (k: string) => api.get(k) as Promise<LaunchMe>);
  useReferralAttribution(api, open ? me : null);

  // No prefill. The board ranks businesses, not people — the owner's
  // Paperclip company name would be the right default, but nothing already
  // mounted here carries it (it's only exposed via the Teams BFF's
  // authenticated `/settings` fetch, which this banner isn't wrapped in),
  // and wiring in a brand-new request off a compact in-product nudge isn't
  // worth it. A wrong default (the Clerk first name) is worse than none —
  // an empty box with a clear placeholder asks the right question instead
  // of something people accept by inertia.
  const [name, setName] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [dismissed, setDismissed] = React.useState(false);
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => {
    if (!user) return;
    try {
      setDismissed(window.localStorage.getItem(dismissKey(user.id)) === "1");
    } catch {
      /* storage unavailable — the banner just stays */
    }
  }, [user]);

  if (!open || !me || !me.onboarded || dismissed || !user) return null;

  const displayName = name.trim();

  const join = async () => {
    if (displayName.length < 2 || busy) return;
    setBusy(true);
    setError(null);
    try {
      const joined = (await api.post("/launch/join", { display_name: displayName.slice(0, 40), public: true })) as LaunchMe;
      await mutate(joined, { revalidate: false });
    } catch (err) {
      setError(joinErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  const dismiss = () => {
    setDismissed(true);
    try {
      window.localStorage.setItem(dismissKey(user.id), "1");
    } catch {
      /* per-session dismiss only */
    }
  };

  const copyLink = async () => {
    if (!me.referral_link) return;
    try {
      await navigator.clipboard.writeText(me.referral_link);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked */
    }
  };

  if (me.joined) {
    return (
      <div className="bg-cream border-b border-info px-4 py-2 text-ui text-info flex items-center gap-3 flex-wrap" data-testid="launch-join-banner">
        <span>
          You&apos;re #{me.effective_position ?? me.position} on the launch sheet. Every referral who subscribes moves you up.
        </span>
        {me.referral_link ? (
          <span className="flex items-center gap-2 min-w-0">
            <span className="font-mono truncate max-w-64">{me.referral_link}</span>
            <button type="button" onClick={copyLink} className="underline">
              {copied ? "Copied" : "Copy"}
            </button>
          </span>
        ) : null}
        <button type="button" onClick={dismiss} aria-label="Dismiss" className="ml-auto underline">
          Dismiss
        </button>
      </div>
    );
  }

  return (
    <div className="bg-cream border-b border-info px-4 py-2 text-ui text-info flex items-center gap-3 flex-wrap" data-testid="launch-join-banner">
      <span>The launch competition is open — first {info.comp_slots.toLocaleString("en-US")} get a month free.</span>
      <div className="flex flex-col gap-0.5">
        <input
          aria-label="Display name"
          value={name}
          maxLength={40}
          placeholder="Your company name"
          onChange={(e) => setName(e.target.value)}
          className="border border-edge rounded px-2 py-0.5 bg-transparent text-foreground w-40"
        />
        <span className="text-2xs text-[var(--midnight-muted)]">Shown on the public board</span>
      </div>
      <button type="button" onClick={join} disabled={busy || displayName.length < 2} className="underline disabled:opacity-50">
        {busy ? "Joining…" : "Join"}
      </button>
      {error ? <span role="alert" className="text-err">{error}</span> : null}
    </div>
  );
}
