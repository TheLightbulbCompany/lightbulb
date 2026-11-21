"use client";

import { useEffect } from "react";

// A Spark referral code: the referrer's Clerk user id without its `user_` prefix.
export const REF_KEY = "lightbulb.os.ref";
export const REF_PATTERN = /^[A-Za-z0-9]{1,64}$/;

// Remembers the `ref` of the latest referral link a person opened, for their first Spark sign-in.
export function RefCapture() {
  useEffect(() => {
    const ref = new URLSearchParams(window.location.search).get("ref");
    if (!ref || !REF_PATTERN.test(ref)) return;
    try {
      localStorage.setItem(REF_KEY, ref);
    } catch {
      // storage blocked: the referral is lost, the page still works
    }
  }, []);
  return null;
}
