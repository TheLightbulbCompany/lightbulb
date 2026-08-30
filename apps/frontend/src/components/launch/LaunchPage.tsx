"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@clerk/nextjs";
import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { fetchLaunchInfo, formatMoney, type LaunchInfo } from "@/lib/launch";
import { LaunchHero } from "./LaunchHero";
import { Leaderboard } from "./Leaderboard";
import { MyLaunchCard } from "./MyLaunchCard";
import "./launch.css";

const rules = (info: LaunchInfo): string[] => [
  "Sign up and finish onboarding — Friday's first run has to complete.",
  `Every entrant puts ${formatMoney(info.pot_per_entrant_cents, "USD")} into the pot; the first ${info.comp_slots} on the waitlist get their month free.`,
  // `GET /launch` carries `referral_jump`; the generic sentence covers the
  // merge→promote window where an older backend still omits it.
  info.referral_jump !== undefined
    ? `Each referral moves you up ${info.referral_jump} spots.`
    : "Referrals move you up the list.",
  "Connect a read-only Stripe key. The board ranks the dollar lift over your prior 30 days.",
  "Judges pick the winner from the board. Self-dealing gets you removed.",
];

export default function LaunchPage() {
  const { isSignedIn } = useAuth();
  const [info, setInfo] = React.useState<LaunchInfo | null | undefined>(undefined);

  React.useEffect(() => {
    let alive = true;
    const load = () => fetchLaunchInfo().then((i) => alive && setInfo(i)).catch(() => alive && setInfo(null));
    load();
    const t = setInterval(load, 30_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      {info === undefined ? (
        <section className="ld-section"><p className="lc-fine">Loading…</p></section>
      ) : info === null ? (
        <section className="ld-section"><p className="ld-lede">The launch is not open yet.</p></section>
      ) : (
        <>
          <LaunchHero info={info}>
            {info.phase === "waitlist" || info.phase === "activation" ? (
              <div className="lc-actions" style={{ justifyContent: "center" }}>
                <Link href="/sign-up" className="ld-btn">Sign up <span className="ld-btn-arrow" aria-hidden="true">→</span></Link>
                {info.phase === "waitlist" ? (
                  <p className="lc-fine" style={{ flexBasis: "100%", textAlign: "center" }}>
                    Already signed up? <Link href="/sign-in">Sign in</Link>
                  </p>
                ) : (
                  <p className="lc-fine" style={{ flexBasis: "100%", textAlign: "center" }}>Got a code in your inbox? Finish onboarding first, then redeem it at checkout.</p>
                )}
              </div>
            ) : null}
          </LaunchHero>

          {info.phase === "live" || info.phase === "ended" ? (
            <section className="ld-section" id="board">
              <p className="ld-kicker">The board</p>
              <h2 className="ld-h2">Revenue lift, <em>live</em>.</h2>
              <Leaderboard />
            </section>
          ) : null}

          {isSignedIn ? (
            <section className="ld-section" id="you">
              <MyLaunchCard info={info} />
            </section>
          ) : null}

          <section className="ld-section" id="rules">
            <p className="ld-kicker">How it works</p>
            <ol className="lc-steps" style={{ fontSize: 15, color: "var(--ink)" }}>
              {rules(info).map((r) => <li key={r}>{r}</li>)}
            </ol>
          </section>
        </>
      )}
      <Footer />
    </main>
  );
}
