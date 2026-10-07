"use client";

import * as React from "react";
import { BACKEND_URL } from "@/lib/api";
import type { LaunchInfo } from "@/lib/launch";

/**
 * Public `GET /launch`, degrading to `null` on ANY non-200 (feature off,
 * transient error) so callers render their launch-free default — shared by
 * LaunchJoinBanner and LaunchJoinModal. State starts `null` and only
 * resolves inside an effect, so there is nothing to mismatch between server
 * and client render. The route sends `Cache-Control: public, max-age=30`,
 * so multiple mounts on one page share the browser cache.
 */
export function useLaunchInfo(): LaunchInfo | null {
  const [info, setInfo] = React.useState<LaunchInfo | null>(null);

  React.useEffect(() => {
    let alive = true;
    fetch(`${BACKEND_URL}/launch`)
      .then((res) => (res.ok ? (res.json() as Promise<LaunchInfo>) : null))
      .then((i) => {
        if (alive) setInfo(i);
      })
      .catch(() => {
        if (alive) setInfo(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  return info;
}
