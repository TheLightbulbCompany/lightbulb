"use client";

import * as React from "react";
import Link from "next/link";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * Typed error card rendered when an agent run terminates with an error.
 *
 * The backend forwards OpenClaw's `errorKind` as `code` on the chat error
 * payload (one of: refusal | timeout | rate_limit | context_length |
 * unknown — see upstream src/infra/errors.ts). We map the known codes to
 * tailored copy + an action button so the user knows what to do next,
 * instead of seeing a generic "Failed to generate" block.
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
      return {
        title: "Provider rate limit reached",
        description:
          "Your model provider hit its usage cap. Three ways to keep going: wait for your quota to reset, upgrade your plan with the provider, or switch this workspace to Isol8's Bedrock-managed credits.",
        action: { label: "Switch to Bedrock", href: "/chat?panel=llm" },
      };
    case "context_length":
      return {
        title: "Conversation too long",
        description:
          message ||
          "The conversation exceeded the model's context window. Start a new chat or pick a model with a larger context.",
        action: { label: "Change model", href: "/chat?panel=llm" },
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
                <Link href={action.href}>{action.label}</Link>
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
