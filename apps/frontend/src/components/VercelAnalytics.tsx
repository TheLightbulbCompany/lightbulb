"use client";

import { Analytics } from "@vercel/analytics/next";
import { withoutChatQuery } from "@/lib/analytics";

/** Vercel Web Analytics (T23): a cookieless visitor count beside PostHog. /chat and /s keep
 *  their query out of it, as they do out of PostHog (file paths, a share link's port). */
export function VercelAnalytics() {
  return <Analytics beforeSend={(event) => ({ ...event, url: withoutChatQuery(event.url) })} />;
}
