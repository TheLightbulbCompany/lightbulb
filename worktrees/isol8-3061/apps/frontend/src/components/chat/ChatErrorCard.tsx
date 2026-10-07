"use client";

import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { handleShellLinkClick } from "@/lib/shellNav";
import { useBilling, type BillingAccount } from "@/hooks/useBilling";

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
 * `included_daily_cap` is the PAYING counterpart: a Base workspace, or a
 * Workspace/Ultra one still on the starter model (no provider chosen), used
 * today's included usage (trial_cap.is_managed_capped). It must never read
 * as a free limit, so its next step comes from the plan: Base upgrades,
 * Workspace/Ultra picks a provider. A `trial_daily_cap` reaching a paid
 * account (a backend that predates the split) renders the same way.
 *
 * `trial_expired` is the sibling local-trial code for the 30-day
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
}

interface ErrorPresentation {
  title: string;
  description: string;
  /** `crossRoute` marks a target on a DIFFERENT Next route (e.g. /settings).
   *  Those must navigate via <Link>; handleShellLinkClick only swaps query
   *  params on the route the shell is already on. See Task 6. */
  action?: { label: string; href: string; crossRoute?: boolean };
}

/** The LLM panel with its change-provider modal open — the provision gate's `provider_required` CTA. */
export const PROVIDER_PICKER = "/chat?panel=llm&action=change-provider";

function describeIncludedCap(account: BillingAccount | null | undefined, message: string): ErrorPresentation {
  const title = "Today's included usage is used up";
  const reset = "You've used today's included usage. It resets at 00:00 UTC.";
  if (account?.plan === "base") {
    return {
      title,
      description: `${reset} Need more today? Upgrade to Workspace for pay-as-you-go credits.`,
      action: { label: "Upgrade", href: "/settings?panel=billing", crossRoute: true },
    };
  }
  if ((account?.plan === "standard" || account?.plan === "ultra_pro") && !account.provider_choice) {
    return {
      title,
      description: `${reset} Need more today? Pick a model provider — ChatGPT or your own key.`,
      action: { label: "Pick provider", href: PROVIDER_PICKER },
    };
  }
  return {
    title,
    description: message || reset,
    action: { label: "Billing", href: "/settings?panel=billing", crossRoute: true },
  };
}

function describeError(
  code: string | undefined,
  message: string,
  account: BillingAccount | null | undefined,
): ErrorPresentation {
  if (code === "included_daily_cap" || (code === "trial_daily_cap" && account?.is_subscribed)) {
    return describeIncludedCap(account, message);
  }
  switch (code) {
    case "rate_limit":
      // Generic "switch provider" framing is wrong here — switching to a BYO
      // key has the same provider-quota problem. The three real remedies are
      // (a) wait for the provider's quota to reset, (b) upgrade your plan
      // with the provider, or (c) move this workspace to Lightbulb's own
      // managed credits, which sidesteps third-party quotas entirely.
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
          "Your model provider hit its usage cap. Three ways to keep going: wait for your quota to reset, upgrade your plan with the provider, or switch this workspace to Lightbulb's managed credits.",
        action: { label: "Switch provider", href: PROVIDER_PICKER },
      };
    case "context_length":
      return {
        title: "Conversation too long",
        description:
          message ||
          "The conversation exceeded the model's context window. Start a new chat or pick a model with a larger context.",
        action: { label: "Change provider", href: PROVIDER_PICKER },
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
          "Your workspace credit balance is used up. Top up to keep your agents running.",
        action: { label: "Top up now", href: "/chat?panel=llm" },
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

export function ChatErrorCard({ message, code }: ChatErrorCardProps) {
  const { account } = useBilling();
  const { title, description, action } = describeError(code, message, account);

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
