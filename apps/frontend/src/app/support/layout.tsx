import type { Metadata } from "next";
import type { ReactNode } from "react";

import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Support | Lightbulb",
  alternates: { canonical: absoluteUrl("/support") },
  description: "Get help with your Lightbulb assistant, your phone line, billing and your account.",
};

export default function SupportLayout({ children }: { children: ReactNode }) {
  return children;
}
