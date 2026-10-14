import type { Metadata } from "next";

import { ThemeStamp } from "@/components/ThemeStamp";

export const metadata: Metadata = {
  title: "Join your team — Lightbulb",
  robots: { index: false, follow: false },
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <ThemeStamp />
      {children}
    </>
  );
}
