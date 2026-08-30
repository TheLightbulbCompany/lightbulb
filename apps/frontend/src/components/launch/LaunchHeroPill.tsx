"use client";

import * as React from "react";
import Link from "next/link";
import { BACKEND_URL } from "@/lib/api";
import { formatMoney, type LaunchInfo } from "@/lib/launch";

/**
 * The landing hero's quiet pointer to the open launch competition — mounted
 * by `HeroFunnel.tsx` directly under the eyebrow. Reads the public `GET
 * /launch` (no auth, same route `LaunchPage`/`fetchLaunchInfo` use), but
 * fetches directly rather than through `fetchLaunchInfo` so a non-200 of any
 * kind (feature off, transient error) degrades the same way: render nothing,
 * never a broken pill on the hero. State starts `null` and only resolves
 * inside an effect, so there is nothing to mismatch between server and
 * client render.
 */
export function LaunchHeroPill() {
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

  if (!info) return null;

  const pot = formatMoney(info.pot_cents, "USD");
  const copy =
    info.phase === "waitlist"
      ? `Launch competition · ${pot} pot · ${info.signups.toLocaleString("en-US")} on the list → see your spot`
      : info.phase === "activation"
        ? `Launch competition · ${pot} pot · doors are open → join`
        : info.phase === "live"
          ? `Launch competition is on · ${info.competing.toLocaleString("en-US")} competing → the board`
          : null;

  if (!copy) return null;

  return (
    <Link href="/launch" className="ld-launch-pill">
      {copy}
    </Link>
  );
}
