import type { Metadata } from "next";
import type { ReactNode } from "react";

import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Support — Lightbulb AI",
  alternates: { canonical: absoluteUrl("/support") },
  description: "Get help with Lightbulb AI workspaces, AI agents, billing, and account support.",
};

export default function SupportLayout({ children }: { children: ReactNode }) {
  return children;
}
