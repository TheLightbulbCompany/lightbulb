"use client";

import { useSearchParams } from "next/navigation";

import { sparkScheme } from "./sparkScheme";

// A Spark referral code: the referrer's Clerk user id without its `user_` prefix.
export const CODE = /^[A-Za-z0-9]{1,64}$/;

// On a referral link (`/os?ref=CODE`), a link that hands the code to the Mac app.
export function OsInvite() {
  const code = useSearchParams()?.get("ref") ?? "";
  if (!CODE.test(code)) return null;
  return (
    <a
      href={`${sparkScheme()}://referral?code=${code}`}
      className="mt-6 inline-block text-sm text-[var(--lb-ink)] underline underline-offset-[3px]"
    >
      Open Spark to apply this invite
    </a>
  );
}
