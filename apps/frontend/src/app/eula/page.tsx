import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";

export const metadata: Metadata = {
  title: "End User License Agreement — isol8",
  alternates: { canonical: "https://isol8.co/eula" },
  description: "The license terms for the isol8 desktop application.",
};

export default function EulaPage() {
  return <LegalDocument dataId="fa22a01d-19e7-4725-ad46-ed18b163a611" />;
}
