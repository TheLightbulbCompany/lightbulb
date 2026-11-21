"use client";

import { capture } from "@/lib/analytics";

import { CODE } from "./OsInvite";

// A plain link (works with JavaScript off); the click is counted, never delayed.
export function SparkDownload() {
  return (
    <a
      href="/download/spark"
      className="lb-btn lb-btn-p lb-btn-xl"
      onClick={() => {
        const ref = new URLSearchParams(window.location.search).get("ref") ?? "";
        // Never the code itself: only whether the visit came from an invite.
        capture("spark_download_clicked", { source: "os_page", ...(CODE.test(ref) && { has_referral: true }) });
      }}
    >
      Download for Mac
    </a>
  );
}
