"use client";

import * as React from "react";
import { fetchLaunchInfo, type LaunchPhase } from "@/lib/launch";

/**
 * The launch phase, read once on mount. `null` while loading, when the
 * feature is off (`/launch` 404s), or on any error — so callers treat
 * `null` as "behave exactly as if there were no competition".
 */
export function useLaunchPhase(): LaunchPhase | null {
  const [phase, setPhase] = React.useState<LaunchPhase | null>(null);
  React.useEffect(() => {
    let alive = true;
    fetchLaunchInfo()
      .then((info) => alive && setPhase(info?.phase ?? null))
      .catch(() => alive && setPhase(null));
    return () => {
      alive = false;
    };
  }, []);
  return phase;
}
