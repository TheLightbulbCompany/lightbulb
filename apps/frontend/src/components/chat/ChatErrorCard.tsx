"use client";

import Link from "next/link";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { handleShellLinkClick } from "@/lib/shellNav";

/**
 * Typed error card rendered when an agent run terminates with an error.
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
 * All four of the codes above are "money codes" (see `MONEY_CODES`): the
 * run died because of the owner's budget/subscription state, not a
 * malfunction, so Retry is suppressed and the card renders from `code`
 * alone — the server `message` is ignored so a pre-send gate_chat refusal
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
  /** Retry handler for the originating assistant bubble. */
  onRetry?: () => void;
}

interface ErrorPresentation {
  title: string;
  description: string;
  /** `crossRoute` marks a target on a DIFFERENT Next route (e.g. /settings).
   *  Those must navigate via <Link>; handleShellLinkClick only swaps query
   *  params on the route the shell is already on. See Task 6. */
  action?: { label: string; href: string; crossRoute?: boolean };
}

/**
 * Codes where the run died because the owner is out of Isol8 budget, not
 * because anything malfunctioned. Retry is suppressed for all of them: it
 * cannot succeed until the cap resets or the owner pays, and offering it
 * makes an exhausted allowance look like a flaky product.
 */
const MONEY_CODES: ReadonlySet<string> = new Set([
  "trial_daily_cap",
  "trial_expired",
  "out_of_credits",
  "subscription_inactive",
]);

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
          "Your model provider hit its usage cap. Three ways to keep going: wait for your quota to reset, upgrade your plan with the provider, or switch this workspace to Isol8's Bedrock-managed credits.",
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

export function ChatErrorCard({ message, code, onRetry }: ChatErrorCardProps) {
  const { title, description, action } = describeError(code, message);
  const showRetry = Boolean(onRetry) && !MONEY_CODES.has(code ?? "");

  return (
    <div
      role="alert"
      data-testid="chat-error-card"
      data-error-code={code ?? "none"}
      className="my-2 rounded-lg border border-[#f5c6cb] bg-[#fdecea] px-4 py-3 text-[#7a2018]"
    >
      <div className="flex items-start gap-3">
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0 text-[#c0392b]" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium leading-tight">{title}</p>
          <p className="mt-1 text-sm leading-snug whitespace-pre-wrap break-words">{description}</p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {action && (
              <Button
                asChild
                size="sm"
                variant="outline"
                className="h-7 px-3 text-xs border-[#c0392b] text-[#7a2018] hover:bg-[#fbd6d2]"
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
            {showRetry && onRetry && (
              <Button
                size="sm"
                variant="ghost"
                className="h-7 px-2 text-xs text-[#7a2018] hover:bg-[#fbd6d2]"
                onClick={onRetry}
              >
                <RefreshCw className="h-3 w-3 mr-1.5" />
                Retry
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
