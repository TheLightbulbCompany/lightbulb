import { Suspense } from "react";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { RequestSetupForm } from "@/components/requestSetup/RequestSetupForm";
import { pageMetadata } from "@/lib/seo";

export const metadata = pageMetadata({
  title: "Talk to a founder | Lightbulb",
  description:
    "Tell us about your salon, spa or barbershop, and one of the founders will set up your AI receptionist with you.",
  path: "/request-setup",
});

export default function RequestSetupPage() {
  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      <section className="mx-auto max-w-2xl px-6 py-20 md:py-28">
        {/* useSearchParams (?source=) needs a Suspense boundary for static rendering. */}
        <Suspense fallback={null}>
          <RequestSetupForm />
        </Suspense>
      </section>
      <Footer />
    </main>
  );
}
