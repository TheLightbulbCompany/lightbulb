import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";

export const metadata: Metadata = {
  title: "Acceptable Use Policy — isol8",
  alternates: { canonical: "https://isol8.co/acceptable-use" },
  description: "The rules governing acceptable use of the isol8 platform.",
};

export default function AcceptableUsePage() {
  return <LegalDocument dataId="03a43af0-03a1-4e9b-83ed-fdd27354bb25" />;
}
