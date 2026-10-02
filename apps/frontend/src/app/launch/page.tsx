import type { Metadata } from "next";
import { Suspense } from "react";
import LaunchPage from "@/components/launch/LaunchPage";
import { pageMetadata } from "@/lib/seo";

// The competition's home is flickmeon.ai (its "/" is rewritten here by the
// middleware). The same page is reachable at /launch on the product domain, so
// the canonical names flickmeon.ai — otherwise search engines file the page
// under the product domain and flickmeon.ai never appears in results.
const LAUNCH_URL = "https://flickmeon.ai";

const base = pageMetadata({
  title: "Lightbulb Launch Competition",
  description: "Build a business with your own AI company — the first 200 on the sheet get a month free, and the biggest revenue lift wins.",
  path: "/launch",
});

export const metadata: Metadata = {
  ...base,
  alternates: { canonical: LAUNCH_URL },
  openGraph: { ...base.openGraph, url: LAUNCH_URL },
};

export default function Page() {
  // useSearchParams (inside the sheet card) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <LaunchPage />
    </Suspense>
  );
}
