import type { Metadata } from "next";
import { Suspense } from "react";
import LaunchPage from "@/components/launch/LaunchPage";
import { pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: "Lightbulb Launch Competition",
  description: "Build a business with your own AI company and compete for the pot on revenue lift.",
  path: "/launch",
});

export default function Page() {
  // useSearchParams (inside the waitlist form / card) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <LaunchPage />
    </Suspense>
  );
}
