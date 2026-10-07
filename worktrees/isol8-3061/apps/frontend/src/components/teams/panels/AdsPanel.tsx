"use client";

// Growth → Ads (`?panel=teams-ads`).
//
// result.dev ships this as a WAITLIST form — spend bracket plus platforms —
// and captures intent before building the rail. Ours is a card until the
// intent capture has a backend row to land in; a form that posts nowhere is
// worse than a sentence. `meta_ads` is wired for the agent, but Meta App
// Review gates live spend, so no prompt: an ad the agent cannot place is not
// an exit.

import { Megaphone } from "lucide-react";
import { SoonPanel } from "./shells/SoonPanel";

export function AdsPanel() {
  return (
    <SoonPanel
      overline="Growth"
      title="Ads"
      icon={Megaphone}
      value="Campaigns on Meta and Google, run by your agents against a budget you set, with spend and results reported here beside your revenue."
    />
  );
}
