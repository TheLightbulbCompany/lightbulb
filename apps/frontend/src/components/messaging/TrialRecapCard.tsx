"use client";

import { useOrganization } from "@clerk/nextjs";
import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { useBilling } from "@/hooks/useBilling";
import { useApi } from "@/lib/api";

/** GET /billing/trial-recap (T19): the team's own numbers on trial days 28-30. */
type Recap =
  | { show: false }
  | {
      show: true;
      trial_end: number;
      handoffs: number;
      approvals: number;
      agents: number;
      people: number;
      top_item: string | null;
    };

const DISMISS_KEY = "lightbulb.trialRecap.dismissedUntil";
const DISMISS_MS = 3 * 86_400_000;

function plural(n: number, one: string, many: string) {
  return `${n} ${n === 1 ? one : many}`;
}

/** The numbers line: a part shows only when its count is above zero. */
export function recapLine(r: Extract<Recap, { show: true }>): string | null {
  const parts = [
    r.handoffs > 0 && plural(r.handoffs, "handoff", "handoffs"),
    r.approvals > 0 && plural(r.approvals, "approval", "approvals"),
    r.agents > 0 &&
      `${plural(r.agents, "agent", "agents")} did work${r.people > 0 ? ` for ${plural(r.people, "person", "people")}` : ""}`,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/**
 * Day-28 recap card at the top of the channel list (docs/marketing/15 §B): value
 * before the ask. Every member sees it; admins get "Keep the team" (Checkout with
 * the card on file, first charge on the trial's own last day, T05), members are
 * pointed at an admin. "Not now" hides it for three days on this browser.
 */
export function TrialRecapCard() {
  const api = useApi();
  const { startCheckout, account } = useBilling();
  const { membership } = useOrganization();
  const isAdmin = membership?.role === "org:admin";
  const { data } = useSWR<Recap>("/billing/trial-recap", (path: string) => api.get(path) as Promise<Recap>);
  const [hidden, setHidden] = useState(() => {
    if (typeof window === "undefined") return false;
    return Number(window.localStorage.getItem(DISMISS_KEY) ?? 0) > Date.now();
  });
  const [pending, setPending] = useState(false);
  if (!data?.show || hidden) return null;

  const line = recapLine(data);
  const nothingFinished = data.handoffs === 0 && data.approvals === 0;
  const price = account?.workspace_price_usd;
  const included = account?.included_members;
  const ends = new Date(data.trial_end * 1000).toLocaleDateString(undefined, {
    timeZone: "UTC",
    month: "long",
    day: "numeric",
  });

  const dismiss = () => {
    window.localStorage.setItem(DISMISS_KEY, String(Date.now() + DISMISS_MS));
    setHidden(true);
  };

  return (
    <section
      className="mx-2 mb-2 rounded-lg border border-[var(--plane-edge)] bg-[var(--plane-raised)] p-3 text-ui"
      aria-label="Your trial so far"
      data-testid="trial-recap"
    >
      <h3 className="font-semibold text-[var(--fg)]">Here&apos;s what your team did in 30 days</h3>
      {nothingFinished ? (
        <p className="mt-1 text-[var(--fg-soft)]">
          Your agents haven&apos;t finished anything for the team yet. Want help setting up one real thread?{" "}
          <Link href="/request-setup" className="text-[var(--link)] underline">
            Book 20 minutes
          </Link>
        </p>
      ) : (
        <>
          {line && <p className="mt-1 text-[var(--fg-soft)]">{line}</p>}
          {data.top_item && <p className="mt-1 text-[var(--fg-soft)]">{data.top_item}</p>}
        </>
      )}
      {price !== undefined && (
        <p className="mt-2 text-[var(--fg)]">
          Keep going for ${price} a month. People are free up to {included ?? "five"}.
        </p>
      )}
      <div className="mt-2 flex items-center gap-3">
        {isAdmin ? (
          <Button
            type="button"
            size="sm"
            disabled={pending}
            onClick={async () => {
              setPending(true);
              try {
                await startCheckout("workspace");
              } finally {
                setPending(false);
              }
            }}
          >
            {pending ? "Starting…" : "Keep the team"}
          </Button>
        ) : (
          <span className="text-[var(--fg-soft)]">Ask an admin to keep the team.</span>
        )}
        <button type="button" className="text-[var(--fg-mute)] underline" onClick={dismiss}>
          Not now
        </button>
      </div>
      <p className="mt-2 text-caption text-[var(--fg-mute)]">
        Your trial ends {ends}. After that everything stays readable; agents stop until someone picks the plan.
      </p>
    </section>
  );
}
