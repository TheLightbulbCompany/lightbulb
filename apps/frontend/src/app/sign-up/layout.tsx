import type { Metadata } from "next";

import { ThemeStamp } from "@/components/ThemeStamp";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Create your team's workspace — Lightbulb",
  description:
    "Bring your team. Create a workspace in a minute, or join the one your teammate set up. 30 days free.",
  alternates: { canonical: absoluteUrl("/sign-up") },
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <ThemeStamp />
      {children}
    </>
  );
}
