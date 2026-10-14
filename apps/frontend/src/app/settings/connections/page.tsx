"use client";

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";

/**
 * /settings/connections — a redirector into Settings → Connections.
 *
 * This URL cannot go away: it is the fixed landing spot for OAuth consent (via
 * /oauth/done) and for the backend's `STRIPE_COMPANY_RETURN_URL`, which is a
 * CDK-set env var on the deployed service. So the route survives, forwarding
 * its whole query string, and ConnectionsPanel owns the `?connected=`/`?error=`
 * /`?stripe*=` return params from there.
 */
export default function ConnectionsPage() {
  // useSearchParams() forces dynamic rendering; the Suspense boundary keeps
  // the static-export step happy, matching /settings/page.tsx.
  return (
    <Suspense fallback={null}>
      <ConnectionsRedirect />
    </Suspense>
  );
}

function ConnectionsRedirect() {
  const router = useRouter();
  const searchParams = useSearchParams();

  useEffect(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.set("panel", "connections");
    router.replace(`/settings?${params.toString()}`);
  }, [router, searchParams]);

  return null;
}
