"use client";

import { useCallback, useState } from "react";
import { PROVIDER_CARDS, type ProviderChoiceId } from "@/lib/providerCards";

export interface TeamBillingCardProps {
  workspacePriceUsd?: number;
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
  onSubscribe?: (provider: ProviderChoiceId) => Promise<void>;
  onManage?: () => Promise<void>;
}

/**
 * The workspace-billing paywall card. Rendered in two places (the /chat
 * blocked screen and the settings billing panel), so it owns no data fetching
 * — the workspace price is passed in from the /billing/account payload, and
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
  workspacePriceUsd,
  ownerRole,
  adminOnly = true,
  isSubscribed = false,
  onSubscribe,
  onManage,
}: TeamBillingCardProps) {
  const [provider, setProvider] = useState<ProviderChoiceId>("openrouter_managed");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canAct = !(adminOnly && ownerRole !== "admin");

  const run = useCallback(
    async (fn?: () => Promise<void>) => {
      if (!fn) return;
      setBusy(true);
      setError(null);
      try {
        await fn();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Something went wrong. Please try again.");
      } finally {
        // A same-plan response stays on this page; never strand its CTA busy.
        setBusy(false);
      }
    },
    [],
  );

  return (
    <div
      data-testid="team-billing-card"
      style={{ fontFamily: "var(--font-sans), sans-serif" }}
      className="w-full max-w-[440px] rounded-xl border border-[var(--edge)] bg-cream px-4 py-5 text-left shadow-sm sm:p-7"
    >
      {/*
        Body copy used to hardcode its own gray, because the old --midnight-muted
        was under WCAG AA on this card — and this paragraph carries the price the
        owner is about to be charged. The manual's --midnight-muted measures
        5.00:1 on cream, so the workaround is retired and this uses the token
        like everything around it.
      */}
      <div className="text-base font-semibold leading-snug text-[var(--midnight)]">Workspace billing</div>
      <p className="mt-1 text-ui leading-snug text-[var(--midnight-muted)]">
        {workspacePriceUsd === undefined ? (
          "Loading workspace price…"
        ) : (
          <>
            <strong className="font-semibold text-[var(--midnight)]">${workspacePriceUsd}/mo</strong> for
            your workspace, up to 5 people; each member from the 6th adds a seat.
          </>
        )}
      </p>
      <div className="mt-4 space-y-2 rounded-lg border border-[var(--edge)] bg-[var(--cream)] px-3.5 py-3 text-ui leading-snug text-[var(--midnight-muted)]">
        <p className="font-semibold text-[var(--midnight)]">Ways to power inference</p>
        {PROVIDER_CARDS.map((card) => (
          <label key={card.id} className="block">
            <input type="radio" name="team-inference-provider" value={card.id} checked={provider === card.id}
              disabled={!canAct || busy} onChange={() => setProvider(card.id)} className="mr-2" />
            <span className="font-medium text-[var(--midnight)]">{card.title}</span>: {card.subtitle}
          </label>
        ))}
      </div>

      {error && (
        <div
          role="alert"
          className="mt-3 rounded-lg border border-err bg-cream px-3.5 py-2.5 text-ui leading-snug text-[var(--err)]"
        >
          {error}
        </div>
      )}

      <div className="mt-4">
        {!canAct ? (
          <p className="rounded-lg border border-[var(--edge)] bg-[var(--cream)] px-3.5 py-3 text-ui leading-snug text-[var(--midnight-muted)]">
            Ask your workspace admin to set up billing. Only a workspace admin can start the
            subscription.
          </p>
        ) : isSubscribed ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(onManage)}
            className="inline-flex min-h-11 items-center justify-center rounded-full border border-[var(--edge)] px-[18px] py-2.5 text-ui font-semibold text-[var(--midnight)] transition-colors hover:border-[var(--midnight-faint)] hover:bg-[var(--cream-deep)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            Manage billing
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(onSubscribe ? () => onSubscribe(provider) : undefined)}
            className="inline-flex min-h-11 items-center justify-center rounded-full bg-[var(--midnight)] px-6 py-2.5 text-sm font-bold text-cream transition-colors hover:bg-[var(--midnight-soft)] disabled:cursor-not-allowed disabled:bg-cream-deep disabled:text-[var(--midnight-muted)] disabled:hover:bg-cream-deep"
          >
            {busy ? "Starting checkout…" : "Subscribe workspace"}
          </button>
        )}
      </div>
    </div>
  );
}
