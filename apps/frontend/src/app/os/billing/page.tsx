import { Suspense } from "react";

import { BillingNotice } from "./BillingNotice";

export const metadata = { title: "Spark billing | Lightbulb", robots: { index: false } };

// Where Spark's checkout and billing links land. Public: the browser that opens them has no sign-in.
export default function OsBillingPage() {
  return (
    <main className="ld-page min-h-screen">
      <section className="mx-auto max-w-2xl px-6 py-20 md:py-28">
        <Suspense>
          <BillingNotice />
        </Suspense>
      </section>
    </main>
  );
}
