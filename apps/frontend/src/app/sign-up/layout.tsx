import type { Metadata } from "next";

import { ThemeStamp } from "@/components/ThemeStamp";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Sign up for Spark | Lightbulb",
  description: "Create your Spark account. Spark is a one-click voice interface for your Mac.",
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
