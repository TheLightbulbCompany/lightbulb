"use client";

import * as React from "react";
import { LogoMark } from "@/components/Logo";
import type { LaunchInfo } from "@/lib/launch";
import { formatMoney } from "@/lib/launch";

const HEADLINE: Record<LaunchInfo["phase"], React.ReactNode> = {
  waitlist: <>The Lightbulb<br />Launch Competition.</>,
  activation: <>Lightbulb is live.<br />The competition starts soon.</>,
  live: <>The competition<br />is on.</>,
  ended: <>The competition<br />has ended.</>,
};

function useCountdown(iso: string): string {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const ms = Math.max(0, new Date(iso).getTime() - now);
  const d = Math.floor(ms / 86_400_000);
  const h = Math.floor((ms % 86_400_000) / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return d > 0 ? `${d}d ${h}h ${m}m` : `${h}h ${m}m ${s}s`;
}

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
        <p className="ld-funnel-fine">Biggest revenue lift over 30 days wins the pot. Every entrant adds $10 to it.</p>
        {children}
        <div className="lc-stats">
          <div className="lc-stat">
            <div className="lc-stat-value">{formatMoney(info.pot_cents, "usd")}</div>
            <div className="ld-mono lc-stat-label">prize pot</div>
          </div>
          <div className="lc-stat">
            <div className="lc-stat-value">{info.phase === "waitlist" ? info.waitlist_count : info.entrants}</div>
            <div className="ld-mono lc-stat-label">{info.phase === "waitlist" ? "on the waitlist" : "entrants"}</div>
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
