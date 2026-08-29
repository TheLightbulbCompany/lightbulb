"use client";

import * as React from "react";
import { LogoMark } from "@/components/Logo";
import type { LaunchInfo } from "@/lib/launch";
import { formatMoney } from "@/lib/launch";
import { useCountdown } from "./useCountdown";

const HEADLINE: Record<LaunchInfo["phase"], React.ReactNode> = {
  waitlist: <>The Lightbulb<br />Launch Competition.</>,
  activation: <>Lightbulb is live.<br />The competition starts soon.</>,
  live: <>The competition<br />is on.</>,
  ended: <>The competition<br />has ended.</>,
};

export function LaunchHero({ info, children }: { info: LaunchInfo; children?: React.ReactNode }) {
  const target = info.phase === "waitlist" ? info.drop_at : info.phase === "activation" ? info.competition_start_at : info.competition_end_at;
  const label = info.phase === "waitlist" ? "until the drop" : info.phase === "activation" ? "until the competition starts" : info.phase === "live" ? "left" : "";
  const countdown = useCountdown(target);
  return (
    <section className="ld-funnel" id="start">
      <div className="ld-funnel-in">
        <p className="ld-mono ld-funnel-eyebrow">Build a business with your own AI company</p>
        <LogoMark className="ld-funnel-mark" />
        <h1 className="ld-funnel-h1">{HEADLINE[info.phase]}</h1>
        <p className="ld-funnel-fine">Biggest revenue lift over 30 days wins the pot. Every entrant adds {formatMoney(info.pot_per_entrant_cents, "USD")} to it.</p>
        {children}
        <div className="lc-stats">
          <div className="lc-stat">
            <div className="lc-stat-value">{formatMoney(info.pot_cents, "USD")}</div>
            <div className="ld-mono lc-stat-label">prize pot</div>
          </div>
          {info.phase === "waitlist" ? (
            <div className="lc-stat">
              <div className="lc-stat-value">{info.signups}</div>
              <div className="ld-mono lc-stat-label">signed up</div>
            </div>
          ) : (
            <>
              <div className="lc-stat">
                <div className="lc-stat-value">{info.competing}</div>
                <div className="ld-mono lc-stat-label">competing</div>
              </div>
              <div className="lc-stat">
                <div className="lc-stat-value">{info.on_board}</div>
                <div className="ld-mono lc-stat-label">on the board</div>
              </div>
            </>
          )}
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
