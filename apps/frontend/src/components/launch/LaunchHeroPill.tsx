"use client";

import * as React from "react";
import Link from "next/link";
import { formatMoney } from "@/lib/launch";
import { useLaunchInfo } from "./useLaunchInfo";

/**
 * The landing hero's quiet pointer to the open launch competition — mounted
 * by `HeroFunnel.tsx` directly under the eyebrow. Reads the public `GET
 * /launch` via `useLaunchInfo`, which degrades any non-200 (feature off,
 * transient error) to `null`: render nothing, never a broken pill on the
 * hero.
 */
export function LaunchHeroPill() {
  const info = useLaunchInfo();

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
