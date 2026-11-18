import type { Metadata } from "next";

import { ThemeStamp } from "@/components/ThemeStamp";
import { absoluteUrl } from "@/lib/seo";

export const metadata: Metadata = {
  title: "Set up your AI receptionist | Lightbulb",
  description:
    "Set up an AI receptionist for your salon, spa or barbershop in five steps. Keep your number. Month to month.",
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
