"use client";

import { useEffect, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { settingsHrefFor, shellSettingsHref } from "./panelRegistry";

/** /chat?panel=<a key the Settings page took> goes to that Settings section
 *  instead of a dead tab. Sits above the shell, so it fires from a hard load, a
 *  link, or an in-shell pushState, and before any onboarding gate. It goes
 *  straight to the Settings place in the shell (UX slice 4). */
export function SettingsRedirect({ children }: { children: ReactNode }) {
  const router = useRouter();
  const settingsHref = settingsHrefFor(useSearchParams().toString());
  const href = settingsHref ? shellSettingsHref(settingsHref) : null;
  useEffect(() => {
    if (href) router.replace(href);
  }, [href, router]);
  return settingsHref ? null : <>{children}</>;
}
