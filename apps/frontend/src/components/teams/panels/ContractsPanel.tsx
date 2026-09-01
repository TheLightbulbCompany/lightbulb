"use client";

// Company → Contracts (`?panel=teams-contracts`). "Coming soon" at
// result.dev too. The agent CAN draft — Brain holds the drafts — but cannot
// send for signature, so the prompt stops at the draft.

import { FileSignature } from "lucide-react";
import { SoonPanel } from "./shells/SoonPanel";

export function ContractsPanel() {
  return (
    <SoonPanel
      overline="Company"
      title="Contracts"
      icon={FileSignature}
      value="Draft, send and countersign the agreements a business runs on — customer terms, contractor agreements, NDAs — without leaving Lightbulb."
      prompt="Draft a mutual NDA for us and save it to the Brain."
    />
  );
}
