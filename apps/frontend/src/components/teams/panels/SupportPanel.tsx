"use client";

// Operations → Support (`?panel=teams-support`).
//
// Blocked on two real things, neither overridden here: there is no
// owner-scoped thread store (`agent_mail_inbox_repo` is address-keyed with no
// thread or open/closed state and no owner index), and spec §4.7 requires a
// prose redactor before customer PII lands in a panel — which does not exist.
// So this is the map entry, not the inbox. The prompt is real: agent mail
// exists and the agent can read and reply to it today.

import { LifeBuoy } from "lucide-react";
import { SoonPanel } from "./shells/SoonPanel";

export function SupportPanel() {
  return (
    <SoonPanel
      overline="Operations"
      title="Support"
      icon={LifeBuoy}
      value="A shared inbox for the people who write to your company — every thread in one place, with Friday drafting replies you approve before they go."
      prompt="Check the company inbox and summarise anything that needs a reply."
    />
  );
}
