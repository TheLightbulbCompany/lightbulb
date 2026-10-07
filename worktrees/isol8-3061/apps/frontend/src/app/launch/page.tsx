import type { Metadata } from "next";
import { Suspense } from "react";
import LaunchPage from "@/components/launch/LaunchPage";
import { pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: "Lightbulb Launch Competition",
  description: "Build a business with your own AI company — the first 200 on the sheet get a month free, and the biggest revenue lift wins.",
  path: "/launch",
});

export default function Page() {
  // useSearchParams (inside the sheet card) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <LaunchPage />
    </Suspense>
  );
}
