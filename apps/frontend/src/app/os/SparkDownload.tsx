"use client";

import { capture } from "@/lib/analytics";

import { CODE } from "./OsInvite";

// Hovering or focusing the button lights the page's bulb (decoration; no listener on the short page).
const light = (on: boolean) => window.dispatchEvent(new CustomEvent("spark:light", { detail: on }));

// A plain link (works with JavaScript off); the click is counted, never delayed. `source` names the
// placement: os_page (the short page), os_hero, os_closing.
export function SparkDownload({ source = "os_page" }: { source?: string }) {
  return (
    <a
      href="/download/spark"
      className="lb-btn lb-btn-p lb-btn-xl"
      onMouseEnter={() => light(true)}
      onMouseLeave={() => light(false)}
      onFocus={() => light(true)}
      onBlur={() => light(false)}
      onClick={() => {
        const ref = new URLSearchParams(window.location.search).get("ref") ?? "";
        // Never the code itself: only whether the visit came from an invite.
        capture("spark_download_clicked", {
          source,
          ...(CODE.test(ref) && { has_referral: true }),
        });
      }}
    >
      Download for Mac
    </a>
  );
}
