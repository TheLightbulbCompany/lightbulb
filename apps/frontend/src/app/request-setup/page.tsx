import { Suspense } from "react";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { RequestSetupForm } from "@/components/requestSetup/RequestSetupForm";
import { pageMetadata } from "@/lib/seo";

export const metadata = pageMetadata({
  title: "Request a guided setup — Lightbulb",
  description:
    "Founding teams of 3 to 15: a founder sets up your workspace with you in 45 minutes. Ten spots.",
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
