"use client";

// Company → Identity (`?panel=teams-identity`).
//
// result.dev's version is a priced form: company type + state → itemised fee
// → "Incorporate my startup", about ten minutes after payment. Ours waits on a
// formation partner and a price; the Stripe-native half (Stripe Identity for
// verification) is unwired too. No prompt — the agent cannot file paperwork.

import { Landmark } from "lucide-react";
import { SoonPanel } from "./shells/SoonPanel";

export function IdentityPanel() {
  return (
    <SoonPanel
      overline="Company"
      title="Identity"
      icon={Landmark}
      value="Make the business a real company: filed with a US state, with an EIN and the documents a bank asks for — tracked here as each step lands."
    />
  );
}
