"use client";

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { shellSettingsHref } from "@/components/workspace/panelRegistry";

// UX slice 4: Settings lives inside the shell. Old /settings URLs and bookmarks open it there, with `?panel=` as its
// section and every other param kept (OAuth/Stripe returns, ?action=).
export default function SettingsPage() {
  // useSearchParams() forces dynamic rendering; wrap the inner component
  // in a Suspense boundary so the static-export step at build time
  // doesn't error (Next.js 16 requirement for static prerender).
  return (
    <Suspense fallback={null}>
      <ToShellSettings />
    </Suspense>
  );
}

function ToShellSettings() {
  const router = useRouter();
  const searchParams = useSearchParams();
  useEffect(() => {
    router.replace(shellSettingsHref(`/settings?${searchParams.toString()}`));
  }, [router, searchParams]);
  return null;
}
