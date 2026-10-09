"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useUser } from "@clerk/nextjs";

type Named = { firstName?: string | null; username?: string | null };

/** Clerk has nothing to call this person: Apple's "Hide My Email", an email-code sign-up. */
export const needsName = (user: Named | null | undefined): boolean =>
  !!user && !user.firstName?.trim() && !user.username;

/**
 * Sends a signed-in person with no name to /welcome/name and brings them back.
 * True while that is happening: the caller holds whatever it was about to do
 * (create a workspace, redeem a join link) so teammates first see a real name.
 * The return path is the path and query only, never the fragment (a join token).
 */
export function useNameGate(enabled = true): boolean {
  const router = useRouter();
  const { isLoaded, user } = useUser();
  const ask = enabled && !!isLoaded && needsName(user);
  useEffect(() => {
    if (!ask) return;
    const here = `${window.location.pathname}${window.location.search}`;
    router.replace(`/welcome/name?returnTo=${encodeURIComponent(here)}`);
  }, [ask, router]);
  return ask;
}
