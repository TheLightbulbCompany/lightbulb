"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useUnifiedNav } from "@/hooks/useUnifiedNav";

// Phase E (flag-gated): under `?unifiedNav=1` the entire `/teams` surface is
// absorbed into the unified `/chat` shell, so flag-on users who land on any
// `/teams/*` route are redirected there. Flag-OFF this is an inert no-op and
// `/teams` renders unchanged — the canonical surface until the flag is the
// default and `/teams` is retired for good.
//
// The flag lives in localStorage (set when the user visits `?unifiedNav=1`),
// so `useUnifiedNav` starts `false` on the server/first render and corrects in
// an effect — meaning the redirect fires client-side after hydration.
export function UnifiedNavRedirect(): null {
  const unified = useUnifiedNav();
  const router = useRouter();

  useEffect(() => {
    if (unified) router.replace("/chat");
  }, [unified, router]);

  return null;
}
