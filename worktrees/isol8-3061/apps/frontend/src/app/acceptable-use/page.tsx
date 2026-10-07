import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Acceptable Use Policy — Lightbulb",
  alternates: { canonical: absoluteUrl("/acceptable-use") },
  description: "The rules governing acceptable use of the Lightbulb platform.",
};

export default function AcceptableUsePage() {
  return <LegalDocument dataId="03a43af0-03a1-4e9b-83ed-fdd27354bb25" />;
}
