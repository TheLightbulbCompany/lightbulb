"use client";

import * as React from "react";
import Link from "next/link";
import { useLaunchInfo } from "./useLaunchInfo";

/**
 * The landing hero's quiet pointer to the launch competition — mounted by
 * `HeroFunnel.tsx` directly under the eyebrow. Reads the public `GET /launch`
 * via `useLaunchInfo`, which degrades any non-200 (feature off, transient
 * error) to `null`: render nothing, never a broken pill on the hero. The
 * closed/ended phases render nothing too — a dead competition earns no hero
 * real estate.
 */
export function LaunchHeroPill() {
  const info = useLaunchInfo();

  if (!info) return null;

  const copy =
    info.phase === "open"
      ? `Launch competition · first ${info.comp_slots.toLocaleString("en-US")} get a month free → see the rules`
      : info.phase === "competition"
        ? "Launch competition · live now → leaderboard"
        : null;

  if (!copy) return null;

  return (
    <Link href="/launch" className="ld-launch-pill">
      {copy}
    </Link>
  );
}
