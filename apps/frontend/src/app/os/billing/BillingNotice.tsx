"use client";

import { useEffect } from "react";
import { useSearchParams } from "next/navigation";

import { sparkScheme } from "../sparkScheme";

const LINES: Record<string, string> = {
  success: "You're subscribed. You can go back to Spark.",
  cancelled: "No changes made. You can go back to Spark.",
  already: "You're already subscribed. You can go back to Spark.",
  portal: "You can go back to Spark.",
};
const EXPIRED = "This link has expired. Open Spark and try again.";

// One line for the `state` the backend sent, and a link back to the Mac app, which it also tries once on load.
// The link carries the state (an unknown one as `expired`), so the app knows a payment just succeeded.
export function BillingNotice() {
  const given = useSearchParams()?.get("state") ?? "";
  const state = Object.hasOwn(LINES, given) ? given : "expired";
  const href = `${sparkScheme()}://account?state=${state}`;
  useEffect(() => {
    window.location.assign(href);
  }, [href]);
  return (
    <>
      <p className="text-lg leading-8 text-[var(--lb-ink)]">{state === "expired" ? EXPIRED : LINES[state]}</p>
      <a href={href} className="mt-6 inline-block text-sm text-[var(--lb-ink)] underline underline-offset-[3px]">
        Open Spark
      </a>
    </>
  );
}
