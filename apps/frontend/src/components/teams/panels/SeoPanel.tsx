"use client";

// Growth → SEO & AEO (`?panel=teams-seo`).
//
// result.dev ships this as a page of named tools, each on its own schedule,
// each reading "Not scanned yet. This one is free and instant, so start
// here." Ours waits for the Exa / Firecrawl / Parallel research rail, which
// the Stripe Projects catalog can provision on the owner's identity. The
// prompt is real today: the agent has a browser and can audit a page.

import { SearchCheck } from "lucide-react";
import { SoonPanel } from "./shells/SoonPanel";

export function SeoPanel() {
  return (
    <SoonPanel
      overline="Growth"
      title="SEO & AEO"
      icon={SearchCheck}
      value="Be the answer in Google and in AI chat. Site health, keyword rankings and whether the AI engines cite you — each checked on its own schedule, so the numbers stay fresh without you clicking."
      prompt="Run a site health check on my website and tell me the three things to fix first."
    />
  );
}
