"use client";

import { useEffect, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useOptionalLiveEvents } from "@/hooks/useLiveEvents";
import { settingsHrefFor, shellSettingsHref } from "./panelRegistry";

/** /chat?panel=<a key the Settings page took> goes to that Settings section
 *  instead of a dead tab. Sits above the shell, so it fires from a hard load, a
 *  link, or an in-shell pushState, and before any onboarding gate. With the
 *  super-agent on (UX slice 4) it goes straight to the Settings place in the
 *  shell, so it waits for the live config to say which. */
export function SettingsRedirect({ children }: { children: ReactNode }) {
  const router = useRouter();
  const live = useOptionalLiveEvents();
  const settingsHref = settingsHrefFor(useSearchParams().toString());
  const waiting = !!settingsHref && !!live?.httpReady && live.featuresLoaded === false;
  const href = !settingsHref || waiting ? null : live?.superAgent ? shellSettingsHref(settingsHref) : settingsHref;
  useEffect(() => {
    if (href) router.replace(href);
  }, [href, router]);
  return settingsHref ? null : <>{children}</>;
}
