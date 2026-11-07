"use client";

// The ONE page-level notice Money shows instead of its Stripe sections:
// unbound, partial, under review, forbidden, or a Stripe read failure.
//
// It is page-level because all three Stripe sections read the same connected
// account through the same envelope — they are blocked for the same reason at
// the same moment, and three copies of that sentence drift invisibly (you only
// ever see one at a time). The failure that motivated it: three tabs saying
// "no payments yet" when the truth was "your Stripe onboarding isn't
// finished", which sends an owner looking for sales they never made instead of
// to the two-minute form that unblocks them.
//
// There is deliberately no `zero` case: a healthy, empty account renders the
// sections, and each one owns its own empty line (MoneyEmptyLine) in its own
// words. `noun` stays because the blocked sentences still have to name what
// the reader came for.

import { CreditCard } from "lucide-react";
import { PanelNotice } from "@/components/teams/shell";
import { handleShellLinkClick } from "@/lib/shellNav";
import type { MoneyView } from "@/hooks/useCompanyMoney";

const ACCOUNTS = "/chat?panel=settings&section=connections";

export function MoneyStates({ view, noun }: { view: MoneyView; noun: string }) {
  switch (view.kind) {
    case "unavailable":
      // A Stripe outage must not render as "no payments". A stale tile is
      // harmless; a list claiming emptiness during an outage is a lie the
      // owner acts on.
      return (
        <PanelNotice
          state="unavailable"
          icon={CreditCard}
          headline={`We couldn't reach Stripe to load your ${noun}.`}
          supporting="This is a temporary read failure, not an empty account. Check back shortly."
        />
      );

    case "forbidden":
      // `company_money._owner()` calls `require_org_admin`, so a non-admin org
      // member 403s on every Money route, forever. Rendering that as a
      // temporary Stripe problem told them to wait for something that never
      // changes. It is permanent, it is ours, and there is no CTA because
      // there is nothing this person can do about it themselves.
      return (
        <PanelNotice
          state="unavailable"
          icon={CreditCard}
          headline={`Only workspace admins can see ${noun}.`}
          supporting="Your workspace admin can share what you need, or change your role."
        />
      );

    case "under_review":
      // details_submitted true, charges_enabled false. The lists are
      // legitimately empty, but "connected and ready" is false — no payment
      // CAN land — and the Accounts panel calls this same state "Under review".
      return (
        <PanelNotice
          state="partial"
          icon={CreditCard}
          headline="Stripe is still reviewing your account."
          supporting={`Your details are submitted, but Stripe hasn't enabled charges yet, so no ${noun} can arrive. This usually clears on its own.`}
          href={ACCOUNTS}
          action={{
            label: "Check status",
            onClick: (e) => handleShellLinkClick(e, ACCOUNTS),
          }}
        />
      );

    case "unbound":
      // Copied deliberately from result.dev, which words this better than we
      // did: it names the upstream dependency and promises the page fills
      // itself in, so the owner knows this tab is not broken, just waiting.
      return (
        <PanelNotice
          state="unbound"
          icon={CreditCard}
          headline="Payments are not set up for this business yet."
          supporting="Once they are, this page fills in on its own."
          prompt="Set up payments for my business."
          href={ACCOUNTS}
          action={{
            label: "Set up payments",
            onClick: (e) => handleShellLinkClick(e, ACCOUNTS),
          }}
        />
      );

    case "partial":
      // The distinction this whole component exists for. `stripe_bound: true`
      // with `details_submitted: false` returns legitimately empty lists, and
      // the owner's next action is a form, not patience. Wording matches the
      // Accounts panel's "Onboarding incomplete" so one company reads as one
      // company across two tabs.
      return (
        <PanelNotice
          state="partial"
          icon={CreditCard}
          headline="Your Stripe onboarding isn't finished."
          // No "either here": for a CONNECTED source the only finish path is
          // Stripe's own Dashboard, opened from Accounts — the in-app
          // "Finish onboarding" button exists only for accounts we created.
          // Verified on the dev fixture: "either here" was a promise the next
          // page could not keep.
          supporting={`Stripe is connected but not yet accepting charges, so there are no ${noun} to show. Finish onboarding in your Stripe Dashboard — you can open it from Accounts.`}
          href={ACCOUNTS}
          // "Open Accounts", not "Finish Stripe setup": CompanyStripeSection
          // gates its "Finish onboarding" button on `source === "created"`, so
          // an owner who CONNECTED an existing unfinished account would be sent
          // to a page with no such button — a CTA promising an action that is
          // not there. Accounts always offers "Open Stripe Dashboard", which
          // works for both sources.
          action={{
            label: "Open Accounts",
            onClick: (e) => handleShellLinkClick(e, ACCOUNTS),
          }}
        />
      );

    // rows / zero / loading: the sections render themselves, including their
    // own empty lines.
    default:
      return null;
  }
}
