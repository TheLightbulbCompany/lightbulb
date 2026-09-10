"use client";

import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { useOrganization } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { handleShellLinkClick } from "@/lib/shellNav";
import { ProviderPicker } from "@/components/chat/ProvisioningStepper";

/**
 * Typed error card rendered when an agent run terminates with an error.
 *
 * There is deliberately NO Retry button. One lived here for a long time,
 * gated on a `Boolean(onRetry)` prop that the only production caller
 * (MessageList) never passed — so it had never rendered once. It was deleted
 * rather than wired, because Retry is truthful for exactly one of the nine
 * states: `timeout`. It repeats the failure for `refusal` and
 * `context_length`, contradicts this card's own advice for `rate_limit`
 * (wait / upgrade / switch — none of which are "press it again"), and for
 * the four budget codes below it makes an exhausted allowance look like a
 * flaky product. Wiring the one honest case needs a resend threaded four
 * layers down through MessageList, which carries no send on its props at
 * all; that is its own change, not a side effect of removing dead code.
 *
 * The backend forwards OpenClaw's `errorKind` as `code` on the chat error
 * payload (one of: refusal | timeout | rate_limit | context_length |
 * unknown — see upstream src/infra/errors.ts). We map the known codes to
 * tailored copy + an action button so the user knows what to do next,
 * instead of seeing a generic "Failed to generate" block.
 *
 * `trial_daily_cap` is a separate, Isol8-side code (not an OpenClaw
 * errorKind) — the gate_chat free-tier $3/day cost cap blocks the turn
 * before it ever reaches OpenClaw (core/gateway/connection_pool.py). It gets
 * its own branch below so a free-tier user sees a distinct "come back
 * tomorrow or upgrade" state rather than a generic failure.
 *
 * `trial_expired` is the sibling Isol8-side code for the 3-day
 * `trial_local` window itself running out (as opposed to the daily cost
 * cap) — surfaced by both the provision gate and gate_chat's Layer 2
 * (core/services/provision_gate.py, core/gateway/connection_pool.py).
 *
 * `out_of_credits` and `subscription_inactive` are the paid-owner
 * counterparts of the two trial codes above — gate_chat emits them when a
 * paying customer's Bedrock credit balance or subscription state blocks the
 * turn. Until this card grew branches for them they fell through to
 * `default`, so a paying customer saw the same "Failed to generate" a crash
 * would produce, with no top-up or reactivate path.
 *
 * All four of the codes above are "money codes": the run died because of the
 * owner's budget/subscription state, not a malfunction, so the card renders
 * from `code` alone — the server `message` is ignored so a pre-send gate_chat refusal
 * and a mid-run reclassification render a pixel-identical card.
 *
 * For unknown / unmapped codes we still show the upstream message
 * verbatim — that is strictly better than masking it with a generic
 * "encountered an error, please try again" string the way the old
 * `friendlyError` regex did.
 */
export interface ChatErrorCardProps {
  /** Raw message text from the backend (already stripped of the "Error: " prefix). */
  message: string;
  /** Optional typed code from OpenClaw's errorKind. */
  code?: string;
  /**
   * Whether this card may render the live conversion paywall. MessageList
   * passes true only for the NEWEST trial-coded card in the transcript —
   * not the newest bubble. The distinction is load-bearing: a mid-run cap
   * emits the reclassified trial_daily_cap error and then OpenClaw's raw
   * "all models rate-limited" frame ~20ms later (verified in prod logs,
   * 17:58:44.187 vs .208), so "newest bubble" demoted the paywall to the
   * settings link in exactly the scenario it was built for. Repeated capped
   * turns still produce one live picker, never a stack. Defaults to true so
   * single-card callers and tests keep the interactive behaviour.
   */
  interactive?: boolean;
}

interface ErrorPresentation {
  title: string;
  description: string;
  /** `crossRoute` marks a target on a DIFFERENT Next route (e.g. /settings).
   *  Those must navigate via <Link>; handleShellLinkClick only swaps query
   *  params on the route the shell is already on. See Task 6. */
  action?: { label: string; href: string; crossRoute?: boolean };
}

function describeError(code: string | undefined, message: string): ErrorPresentation {
  switch (code) {
    case "rate_limit":
      // Generic "switch provider" framing is wrong here — switching to a BYO
      // key has the same provider-quota problem. The three real remedies are
      // (a) wait for the provider's quota to reset, (b) upgrade your plan
      // with the provider, or (c) move this workspace to Isol8's
      // Bedrock-managed credits, which sidesteps third-party quotas entirely.
      // Upstream `message` ("The usage limit has been reached.") isn't
      // actionable, so we always use our own copy here.
      //
      // The action URL carries ``action=change-provider`` so LLMPanel
      // auto-opens the change-provider modal on mount — without that the
      // user lands on a static panel showing their current provider with
      // no obvious "what now" surface (the original bug that motivated
      // this flow).
      return {
        title: "Provider rate limit reached",
        description:
          "Your model provider hit its usage cap. Three ways to keep going: wait for your quota to reset, upgrade your plan with the provider, or switch this workspace to Lightbulb's Bedrock-managed credits.",
        action: { label: "Switch provider", href: "/chat?panel=llm&action=change-provider" },
      };
    case "context_length":
      return {
        title: "Conversation too long",
        description:
          message ||
          "The conversation exceeded the model's context window. Start a new chat or pick a model with a larger context.",
        action: { label: "Change provider", href: "/chat?panel=llm&action=change-provider" },
      };
    case "timeout":
      return {
        title: "Provider timeout",
        description:
          message ||
          "The model provider took too long to respond. This usually clears up on its own — try again in a moment.",
      };
    case "refusal":
      return {
        title: "Request refused",
        description:
          message ||
          "The model declined to respond, usually due to content policy. Try rephrasing your message.",
      };
    case "trial_daily_cap":
      // Copy is derived from `code` alone — the server message is ignored, so
      // the pre-send refusal (gate_chat) and the mid-run reclassification
      // render an identical card. No dollar figure: the wall fires at
      // CAP - RESERVE while Settings advertises the headline $3/day.
      //
      // No "in about N hours" countdown: it's arithmetically correct but
      // surprising near the rollover (e.g. "in about 24 hours" 20 minutes
      // after UTC midnight, if the cap is re-hit immediately), and dropping
      // it also removes the client-clock dependency entirely — a deliberate
      // human product call, not a bug fix.
      return {
        title: "Daily free limit reached",
        description: "You've used today's free usage. Resets at midnight UTC.",
        action: {
          label: "Upgrade to keep going",
          href: "/settings?panel=billing",
          crossRoute: true,
        },
      };
    case "trial_expired":
      // No reset line: trial_cap._is_expired never self-clears.
      return {
        title: "Your free trial has ended",
        description: "Your free trial has ended. Choose a plan to keep your agent running.",
        action: {
          label: "Choose a plan",
          href: "/settings?panel=billing",
          crossRoute: true,
        },
      };
    case "out_of_credits":
      // Previously fell through to `default` — a paying customer saw the same
      // "Failed to generate" a crash would produce, with no way to top up.
      return {
        title: "Out of credits",
        description:
          "Your Claude credit balance is used up. Top up to keep your agents running.",
        // In-shell: routes into CreditsStep, same as OutOfCreditsBanner.
        action: { label: "Top up now", href: "/chat?provider=bedrock_claude" },
      };
    case "subscription_inactive":
      return {
        title: "Subscription inactive",
        description:
          "Your subscription isn't active, so agent runs are paused. Reactivate to continue.",
        action: {
          label: "Reactivate",
          href: "/settings?panel=billing",
          crossRoute: true,
        },
      };
    case "unknown":
    default:
      return {
        title: "Failed to generate",
        description: message || "Your agent encountered an error. Please try again.",
      };
  }
}

/**
 * The two free-tier codes convert IN PLACE instead of linking out.
 *
 * These used to render as a red error card whose action cross-routed to
 * /settings?panel=billing. Two things were wrong with that. First, running
 * out of free usage is not a malfunction — it is the moment the product
 * worked — and a red alert box frames the one success signal you get as a
 * failure. Second, /settings was the ONLY paywall a mid-session owner ever
 * reached: `useOnboardingStatus` polls "0/off once ready", so nothing
 * re-asks the gate after the shell settles, `gatePhase` never flips to
 * blocked, and ProvisioningStepper's own picker branch stays unreachable
 * for the entire session. The good screen existed and no one could get to it.
 *
 * Rendering the picker here — rather than flipping the gate so the stepper
 * takes over — is deliberate. ProvisioningStepper WRAPS {dock}
 * (WorkspaceShell.tsx), so a blocked gate swaps the whole Dockview workspace
 * for the paywall: panels, layout, open detail views and scroll positions
 * all unmount mid-session. That teardown already has a name in this codebase
 * (`DockUnmountSentinel`, "the recovery regression"). Doing it deliberately
 * at the exact moment someone is deciding whether to pay is the worst
 * possible time for it.
 */
const TRIAL_PAYWALL_HEADINGS: Readonly<Record<string, string>> = {
  trial_daily_cap: "You've used today's free usage",
  trial_expired: "Your free trial has ended",
};

/**
 * Whether this error code renders the in-place conversion paywall. Exported
 * for MessageList, which must decide WHICH card in the transcript gets the
 * live picker — the two must agree on what counts as a paywall code, or the
 * card MessageList blesses and the card that can actually render a picker
 * drift apart.
 */
export function isTrialPaywallCode(code: string | undefined | null): boolean {
  return Boolean(code) && Object.hasOwn(TRIAL_PAYWALL_HEADINGS, code as string);
}

export function ChatErrorCard({ message, code, interactive = true }: ChatErrorCardProps) {
  // ProviderPicker exposes checkout only in a workspace context. An org should
  // never carry a trial_local row today, but ProvisioningStepper's own picker
  // branch guards this too.
  // This is a new consequence site. Org context falls through to the linked
  // card below, whose /settings target renders TeamBillingCard correctly.
  // `isLoaded` matters, not just `organization`: Clerk reports
  // `organization: null` while it is still loading, so keying on the value
  // alone treats "don't know yet" as "no org" and would render the personal
  // picker for one paint — the path an org admin must not take. Withhold the
  // paywall until Clerk has actually answered; the linked card below is the
  // safe thing to show in the meantime.
  const { organization, isLoaded } = useOrganization();
  // Object.hasOwn, not a bare index: `code` is server-supplied, and indexing a
  // plain object literal with it resolves inherited Object.prototype members
  // ("constructor", "toString") to truthy non-string values that sail through
  // the gate below — rendering a purchase form for a code that is not a trial
  // code at all.
  const paywallHeading =
    isLoaded && !organization && code && Object.hasOwn(TRIAL_PAYWALL_HEADINGS, code)
      ? TRIAL_PAYWALL_HEADINGS[code]
      : undefined;
  // `interactive` is false for every error bubble except the newest, so a
  // transcript with several capped turns shows ONE live paywall instead of N.
  // Each ProviderPicker owns its own checkout loading state, so stacked
  // copies would expose competing subscription actions in one transcript.
  if (paywallHeading && interactive) {
    return (
      // Deliberately NOT role="alert" (implicit aria-live="assertive" +
      // aria-atomic="true"). That was right for the static error this branch
      // replaced and wrong for a live-updating checkout surface.
      <section
        aria-label={paywallHeading}
        data-testid="chat-error-card"
        data-error-code={code ?? "none"}
        className="my-2 rounded-lg border border-[var(--edge)] bg-cream px-4 py-4"
      >
        <ProviderPicker
          embedded
          isOrg={false}
          source="trial_conversion_inline"
          heading={paywallHeading}
          subheading={
            code === "trial_daily_cap"
              ? "Your agents are paused until midnight UTC. Subscribe to lift the cap and keep them running."
              : "Your agents are paused. Subscribe to start them again."
          }
        />
      </section>
    );
  }

  const { title, description, action } = describeError(code, message);

  return (
    <div
      role="alert"
      data-testid="chat-error-card"
      data-error-code={code ?? "none"}
      className="my-2 rounded-lg border border-err bg-cream px-4 py-3 text-err"
    >
      <div className="flex items-start gap-3">
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0 text-err" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium leading-tight">{title}</p>
          <p className="mt-1 text-sm leading-snug whitespace-pre-wrap break-words text-[var(--midnight-soft)]">{description}</p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {action && (
              <Button
                asChild
                size="sm"
                variant="outline"
                className="h-7 px-3 text-xs border-err text-err hover:bg-[var(--cream-deep)]"
              >
                {action.crossRoute ? (
                  // Different Next route — must go through the router. A bare
                  // pushState would move the URL bar and leave the shell mounted.
                  <Link href={action.href}>{action.label}</Link>
                ) : (
                  <a href={action.href} onClick={(e) => handleShellLinkClick(e, action.href)}>
                    {action.label}
                  </a>
                )}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
