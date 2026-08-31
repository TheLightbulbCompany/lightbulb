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
 * no navigation — with a display-name field prefilled from the Clerk profile
 * (POST /launch/join REQUIRES display_name). After joining it shows the
 * position + referral link, and only then becomes dismissible (per-user
 * localStorage key): the join nudge itself stays until they join, /launch
 * has the full rules.
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

  const prefill = user?.firstName || user?.primaryEmailAddress?.emailAddress.split("@")[0] || "";
  const [name, setName] = React.useState<string | null>(null); // null = untouched, use prefill
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

  const displayName = (name ?? prefill).trim();

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
      <input
        aria-label="Display name"
        value={name ?? prefill}
        maxLength={40}
        onChange={(e) => setName(e.target.value)}
        className="border border-edge rounded px-2 py-0.5 bg-transparent text-foreground w-40"
      />
      <button type="button" onClick={join} disabled={busy || displayName.length < 2} className="underline disabled:opacity-50">
        {busy ? "Joining…" : "Join"}
      </button>
      {error ? <span role="alert" className="text-err">{error}</span> : null}
    </div>
  );
}
