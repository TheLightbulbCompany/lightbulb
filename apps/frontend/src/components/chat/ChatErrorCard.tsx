"use client";

import * as React from "react";
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
  action?: { label: string; href: string };
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
      return {
        title: "Daily free limit reached",
        description:
          message ||
          "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
        // The upgrade *action* (checkout) is a later phase — for now this
        // just deep-links to the Billing settings panel, same pattern as
        // TrialBanner's "Manage" link.
        action: { label: "Upgrade", href: "/settings?panel=billing" },
      };
    case "trial_expired":
      return {
        title: "Your free trial has ended",
        description:
          message || "Your 3-day free trial has ended. Pick a plan to keep your agent.",
        action: { label: "Upgrade", href: "/settings?panel=billing" },
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
                <a href={action.href} onClick={(e) => handleShellLinkClick(e, action.href)}>
                  {action.label}
                </a>
              </Button>
            )}
            {onRetry && (
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
