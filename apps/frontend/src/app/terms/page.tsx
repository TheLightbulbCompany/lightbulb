import type { Metadata } from "next";

import { LegalDocument } from "@/components/legal/LegalDocument";

export const metadata: Metadata = {
  title: "Terms & Conditions — isol8",
  description: "The terms and conditions governing your use of isol8.",
};

export default function TermsPage() {
  return <LegalDocument dataId="10d6963d-d6ae-4556-9b11-16442098290c" />;
}
