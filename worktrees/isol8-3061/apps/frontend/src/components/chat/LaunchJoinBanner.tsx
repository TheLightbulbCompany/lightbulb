"use client";

import * as React from "react";
import useSWR from "swr";
import { useUser } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { useLaunchInfo } from "@/components/launch/useLaunchInfo";
import { useReferralAttribution } from "@/components/launch/useReferralAttribution";
import type { LaunchMe } from "@/lib/launch";

/**
 * The in-product competition-sheet status strip: position + referral link for
 * an owner who has ALREADY joined, dismissible per user.
 *
 * It no longer asks anyone to join — {@link LaunchJoinModal} owns that now,
 * for two reasons. The join form here was gated on `me.onboarded`, so it was
 * invisible for the whole provisioning window; and because `/launch/me` is
 * read once per page load with focus revalidation off app-wide (`SwrProvider`),
 * nothing ever re-asked once Friday's first run landed. The strip only
 * appeared after a full reload, which is exactly how it was reported. Beyond
 * the bug, a slim line beside two other banners was losing to everything else
 * on the page: 200 comped months on offer, three signups taken.
 *
 * So the ask is one deliberate modal and this is the receipt. It still mounts
 * unconditionally in both shells because it also carries
 * `useReferralAttribution`, which must run for every signed-in owner whether
 * or not they ever join.
 */

function dismissKey(userId: string): string {
  return `launch_sheet_banner_dismissed:${userId}`;
}

export function LaunchJoinBanner() {
  const api = useApi();
  const { user } = useUser();
  const info = useLaunchInfo();
  const open = info?.phase === "open";
  // Same SWR key as LaunchJoinModal, so joining there flips this strip to its
  // joined state in the same render — no refetch, no reload.
  const { data: me } = useSWR<LaunchMe>(open ? "/launch/me" : null, (k: string) => api.get(k) as Promise<LaunchMe>);
  useReferralAttribution(api, open ? me : null);

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

  const dismiss = () => {
    setDismissed(true);
    try {
      if (user) window.localStorage.setItem(dismissKey(user.id), "1");
    } catch {
      /* per-session dismiss only */
    }
  };

  const copyLink = async () => {
    if (!me?.referral_link) return;
    try {
      await navigator.clipboard.writeText(me.referral_link);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked */
    }
  };

  if (!open || !me || !me.joined || dismissed || !user) return null;

  return (
    <div
      className="bg-cream border-b border-info px-4 py-2 text-ui text-info flex items-center gap-3 flex-wrap"
      data-testid="launch-join-banner"
    >
      <span>
        You&apos;re #{me.effective_position ?? me.position} on the launch sheet. Every referral who subscribes moves you
        up.
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
