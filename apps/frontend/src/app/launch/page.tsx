import type { Metadata } from "next";
import { Suspense } from "react";
import LaunchPage from "@/components/launch/LaunchPage";
import { pageMetadata } from "@/lib/seo";

// flickmeon.ai now 308s everywhere (the competition is not set up), so this
// page is reachable only at /launch on the product site, and kept out of the
// index until the competition is real.
export const metadata: Metadata = {
  ...pageMetadata({
    title: "Lightbulb Launch Competition",
    description: "Build a business with your own AI company — the first 200 on the sheet get a month free, and the biggest revenue lift wins.",
    path: "/launch",
  }),
  robots: { index: false, follow: true },
};

export default function Page() {
  // useSearchParams (inside the sheet card) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <LaunchPage />
    </Suspense>
  );
}
