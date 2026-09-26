import { NextResponse } from "next/server";

// The build this deployment serves. A tab compares it with the build it was
// loaded from (components/DeployRefresh.tsx). no-store: a cached answer would
// hide the very deploy it exists to announce.
export const dynamic = "force-dynamic";

export function GET(): NextResponse {
  return NextResponse.json(
    { buildId: process.env.NEXT_PUBLIC_BUILD_ID },
    { headers: { "Cache-Control": "no-store" } },
  );
}
