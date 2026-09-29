import type { Metadata } from "next";

import { DEEP_LINK_SCHEME } from "@/lib/domain";

/**
 * Landing page for the Stripe app-to-web Checkout return.
 *
 * On iOS this URL is a Universal Link: when the app is installed, iOS
 * intercepts it and hands control back to the app, and this page is never
 * seen. It exists for the cases where that does NOT happen — and Apple's own
 * docs are clear those cases are routine, not exceptional:
 *
 *   - the customer completed checkout on a desktop browser
 *   - the app is not installed on the device they paid from
 *   - iOS declined to open the app (association not yet fetched, link opened
 *     from a context that suppresses Universal Links, developer-mode quirks)
 *
 * Without this page those customers land on a 404 immediately after paying us,
 * which is the worst possible moment to show one. The subscription itself is
 * already safe either way — it is granted by the `customer.subscription.*`
 * webhook, never by anyone reaching this page.
 *
 * The route is `[status]` rather than two files so the Checkout `success_url`
 * and `cancel_url` can differ by path alone.
 */

export const metadata: Metadata = {
  title: "Checkout — Lightbulb",
  // Nothing here should ever be indexed: these URLs are per-transaction
  // return targets, not content.
  robots: { index: false, follow: false },
};

const COPY: Record<string, { heading: string; body: string }> = {
  success: {
    heading: "You're subscribed.",
    body: "Payment received. Your plan is active — head back to the Lightbulb app and your agents will pick it up within a few seconds.",
  },
  cancel: {
    heading: "Checkout cancelled.",
    body: "Nothing was charged. You can head back to the Lightbulb app and start again whenever you like.",
  },

  // Integration OAuth started from a native app (`return_to=app` on
  // /connections/{provider}/authorize) lands here too, so the consent browser
  // sends the user back to the app instead of into the web workspace.
  connected: {
    heading: "Connected.",
    body: "Your connection is ready. Head back to the Lightbulb app — your agents can use it now.",
  },
  connect_failed: {
    heading: "Connection failed.",
    body: "Nothing was connected. Head back to the Lightbulb app and try again.",
  },

  portal: {
    heading: "Billing updated.",
    body: "Any changes you made have been saved. Head back to the Lightbulb app — your plan will reflect them within a few seconds.",
  },
};

const FALLBACK = {
  heading: "Checkout complete.",
  body: "You can head back to the Lightbulb app.",
};

export default async function CheckoutRedirectPage({
  params,
}: {
  params: Promise<{ status: string }>;
}) {
  const { status } = await params;
  const copy = COPY[status] ?? FALLBACK;

  return (
    <div className="legal-page">
      <main className="legal-content">
        <h1>{copy.heading}</h1>
        <p>{copy.body}</p>
        {/* Custom-scheme fallback, per Apple's guidance that a universal link
            can fail to open the app. Harmless on desktop, where it simply
            does nothing. */}
        <p>
          <a href={`${DEEP_LINK_SCHEME}://`}>Open the Lightbulb app</a>
        </p>
      </main>
    </div>
  );
}
