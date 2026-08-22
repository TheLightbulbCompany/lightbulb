import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";

export const metadata: Metadata = {
  title: "Disclaimer — isol8",
  alternates: { canonical: "https://isol8.co/disclaimer" },
  description: "Disclaimers governing your use of isol8 and its AI agents.",
};

export default function DisclaimerPage() {
  return <LegalDocument dataId="9225fb57-aa0e-4aad-abb7-f8f7078163ce" />;
}
