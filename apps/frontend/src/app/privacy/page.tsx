import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Privacy Policy — Lightbulb",
  alternates: { canonical: absoluteUrl("/privacy") },
  description: "How Lightbulb collects, uses, stores, and protects your personal information.",
};

export default function PrivacyPage() {
  return <LegalDocument dataId="906a65b7-d5af-402a-8b3b-9c82ffef2a86" />;
}
