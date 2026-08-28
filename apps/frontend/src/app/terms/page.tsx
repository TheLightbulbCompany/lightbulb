import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Terms & Conditions — Lightbulb",
  alternates: { canonical: absoluteUrl("/terms") },
  description: "The terms and conditions governing your use of Lightbulb.",
};

export default function TermsPage() {
  return <LegalDocument dataId="10d6963d-d6ae-4556-9b11-16442098290c" />;
}
