import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Disclaimer — Lightbulb",
  alternates: { canonical: absoluteUrl("/disclaimer") },
  description: "Disclaimers governing your use of Lightbulb and its AI agents.",
};

export default function DisclaimerPage() {
  return <LegalDocument dataId="9225fb57-aa0e-4aad-abb7-f8f7078163ce" />;
}
