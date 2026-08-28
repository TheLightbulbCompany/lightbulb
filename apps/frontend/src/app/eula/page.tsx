import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "End User License Agreement — Lightbulb",
  alternates: { canonical: absoluteUrl("/eula") },
  description: "The license terms for the Lightbulb desktop application.",
};

export default function EulaPage() {
  return <LegalDocument dataId="fa22a01d-19e7-4725-ad46-ed18b163a611" />;
}
