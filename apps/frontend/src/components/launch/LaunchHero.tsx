"use client";

import * as React from "react";
import { LogoMark } from "@/components/Logo";
import type { LaunchInfo } from "@/lib/launch";
import { useCountdown } from "./useCountdown";

const HEADLINE: Record<LaunchInfo["phase"], React.ReactNode> = {
  open: <>The Lightbulb<br />Launch Competition.</>,
  closed: <>The sheet is closed.<br />The competition starts soon.</>,
  competition: <>The competition<br />is on.</>,
  ended: <>The competition<br />has ended.</>,
};

// NO pot anywhere on this page — the pot is admin-only.
export function LaunchHero({ info, children }: { info: LaunchInfo; children?: React.ReactNode }) {
  const target =
    info.phase === "open"
      ? info.sheet_closes_at
      : info.phase === "closed"
        ? info.competition_start_at
        : info.competition_end_at;
  const label =
    info.phase === "open"
      ? "until the sheet closes"
      : info.phase === "closed"
        ? "until the competition starts"
        : info.phase === "competition"
          ? "left"
          : "";
  const countdown = useCountdown(target) ?? "—";
  return (
    <section className="ld-funnel" id="start">
      <div className="ld-funnel-in">
        <p className="ld-mono ld-funnel-eyebrow">Build a business with your own AI company</p>
        <LogoMark className="ld-funnel-mark" />
        <h1 className="ld-funnel-h1">{HEADLINE[info.phase]}</h1>
        <p className="ld-funnel-fine">
          The first {info.comp_slots.toLocaleString("en-US")} on the sheet get their first month free, and the biggest
          revenue lift over the competition wins.
        </p>
        {children}
        <div className="lc-stats">
          <div className="lc-stat">
            <div className="lc-stat-value">{info.signups.toLocaleString("en-US")}</div>
            {/* "signups", never "on the sheet" — the counter includes attribution-only rows. */}
            <div className="ld-mono lc-stat-label">signups</div>
          </div>
          {info.phase !== "ended" ? (
            <div className="lc-stat">
              <div className="lc-stat-value">{countdown}</div>
              <div className="ld-mono lc-stat-label">{label}</div>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
