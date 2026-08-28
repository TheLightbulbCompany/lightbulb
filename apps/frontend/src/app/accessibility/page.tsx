import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Accessibility Statement — Lightbulb",
  alternates: { canonical: absoluteUrl("/accessibility") },
  description: "Lightbulb's commitment to digital accessibility and WCAG 2.2 AA.",
};

export default function AccessibilityPage() {
  return <LegalDocument dataId="3eb0d2ad-94d8-48d4-a51b-7de55c42e14c" />;
}
