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
 *
 * The open-phase copy leads with the reader's business, not with a discount:
 * a visitor who has just landed does not yet care enough about us to value a
 * free month, and "get a month free" is the most worn phrase in the category.
 * It names no pot and no dollar figure — the pot is admin-only on every
 * public surface (`lib/launch.ts`, `schemas/launch.py`), which is asserted by
 * this component's own test.
 */
export function LaunchHeroPill() {
  const info = useLaunchInfo();

  if (!info) return null;

  const copy =
    info.phase === "open"
      ? "Launch competition · whoever grows their business most, wins → see the rules"
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
