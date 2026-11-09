"use client";

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";

// Old bookmarks and billing/OAuth returns use the current Settings place. Preserve
// checkout and provider intent; the retired chat shell no longer mounts Settings.
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
    const query = searchParams.toString();
    router.replace(`/voice/settings${query ? `?${query}` : ""}`);
  }, [router, searchParams]);
  return null;
}
