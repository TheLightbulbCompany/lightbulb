import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Cookie Policy — isol8",
  alternates: { canonical: absoluteUrl("/cookies") },
  description: "How isol8 uses cookies and similar tracking technologies.",
};

export default function CookiesPage() {
  return <LegalDocument dataId="4a26e6a0-c9d8-47db-ad53-934f4a4405b1" />;
}
