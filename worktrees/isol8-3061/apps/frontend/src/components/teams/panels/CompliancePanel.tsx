"use client";

// Company → Compliance (`?panel=teams-compliance`). "Coming soon" at
// result.dev too. No rail, no prompt.

import { ShieldCheck } from "lucide-react";
import { SoonPanel } from "./shells/SoonPanel";

export function CompliancePanel() {
  return (
    <SoonPanel
      overline="Company"
      title="Compliance"
      icon={ShieldCheck}
      value="The policies a business is asked for before it can sell — privacy, terms, cookies, data requests — written for what you actually collect, and kept current as it changes."
    />
  );
}
