"use client";

import type { ReactElement } from "react";
import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { describeError } from "@/components/chat/ChatErrorCard";
import { Button } from "@/components/ui/button";
import { handleShellLinkClick } from "@/lib/shellNav";
import type { BillingAccount } from "@/hooks/useBilling";

/** What the block means for the room: agent turns stop, people can still talk. */
const AGENTS_LINE: Partial<Record<NonNullable<BillingAccount["block_reason"]>, string>> = {
  trial_expired: "Your team's record is all here, and people can still message. Agents start again when an admin picks the plan.",
  trial_daily_cap: "Agents are paused until it resets at 00:00 UTC.",
  included_daily_cap: "Agents are paused until it resets at 00:00 UTC.",
};

/**
 * Pinned above the composer while the owner's agents cannot run (the backend's
 * `block_reason`). Title and CTA come from ChatErrorCard so the bar and the card
 * never disagree. The composer stays enabled on purpose: rooms are multiplayer,
 * and a message to a person still goes through; only agent turns are refused.
 */
export function RoomLimitBar({ account }: { account?: BillingAccount | null }): ReactElement | null {
  const reason = account?.block_reason;
  // out_of_credits is the shell's OutOfCreditsBanner's job, not the room's.
  const line = reason && AGENTS_LINE[reason];
  if (!reason || !line) return null;
  const { title, action } = describeError(reason, "", account);
  return <div className="room-limit-bar" role="status" data-block-reason={reason}>
    <AlertTriangle className="size-4 shrink-0 text-err" aria-hidden="true" />
    <p className="min-w-0 flex-1 text-sm">
      <span className="font-medium">{title}.</span> {line}
      {reason === "trial_expired" ? "" : " Messages to people still send."}
    </p>
    {action && <Button asChild size="sm" variant="outline">
      {action.crossRoute
        ? <Link href={action.href}>{action.label}</Link>
        : <a href={action.href} onClick={(event) => handleShellLinkClick(event, action.href)}>{action.label}</a>}
    </Button>}
  </div>;
}
