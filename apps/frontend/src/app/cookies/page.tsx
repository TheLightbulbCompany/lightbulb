import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Cookie Policy — Lightbulb",
  alternates: { canonical: absoluteUrl("/cookies") },
  description: "How Lightbulb uses cookies and similar tracking technologies.",
};

export default function CookiesPage() {
  return <LegalDocument dataId="bec2e1d1-20d9-4929-95f2-a14baed522a5" />;
}
