"use client";

import * as React from "react";

/**
 * Live "Xd Yh Zm" (or "Yh Zm Ws" under a day) countdown to `iso`, floored at
 * zero. Returns `null` until the client clock is read inside an effect —
 * reading `Date.now()` at render time would render different text on the
 * server prerender than on client hydration. Consumers should render a
 * stable placeholder (e.g. "—") while the result is `null`.
 */
export function useCountdown(iso: string): string | null {
  const [now, setNow] = React.useState<number | null>(null);
  React.useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  if (now === null) return null;
  const ms = Math.max(0, new Date(iso).getTime() - now);
  const d = Math.floor(ms / 86_400_000);
  const h = Math.floor((ms % 86_400_000) / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return d > 0 ? `${d}d ${h}h ${m}m` : `${h}h ${m}m ${s}s`;
}
