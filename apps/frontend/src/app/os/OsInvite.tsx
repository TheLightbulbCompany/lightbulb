"use client";

import { useSearchParams } from "next/navigation";

// A Spark referral code: the referrer's Clerk user id without its `user_` prefix.
const CODE = /^[A-Za-z0-9]{1,64}$/;

// The Mac app's URL scheme: the release app owns `spark://`; the dev build, used with the dev site, `spark-dev://`.
function scheme() {
  const host = typeof window === "undefined" ? "" : window.location.hostname;
  return host === "localhost" || host.startsWith("dev.") ? "spark-dev" : "spark";
}

// On a referral link (`/os?ref=CODE`), a link that hands the code to the Mac app.
export function OsInvite() {
  const code = useSearchParams()?.get("ref") ?? "";
  if (!CODE.test(code)) return null;
  return (
    <a
      href={`${scheme()}://referral?code=${code}`}
      className="mt-6 inline-block text-sm text-[var(--lb-ink)] underline underline-offset-[3px]"
    >
      Open Spark to apply this invite
    </a>
  );
}
