"use client";

import * as React from "react";
import { useAuth } from "@clerk/nextjs";
import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { SITE_URL } from "@/lib/domain";
import { fetchLaunchInfo, type LaunchInfo } from "@/lib/launch";
import { LaunchHero } from "./LaunchHero";
import { Leaderboard } from "./Leaderboard";
import { SheetCard } from "./SheetCard";
import { TeamInterestForm } from "./TeamInterestForm";
import "./launch.css";

const ET = "America/New_York";

/** "September 30 at 12:00 PM ET" — the rule copy renders the payload's own
 * instants, never hardcoded dates. */
function etDateTime(iso: string): string {
  const d = new Date(iso);
  const day = new Intl.DateTimeFormat("en-US", { timeZone: ET, month: "long", day: "numeric" }).format(d);
  const time = new Intl.DateTimeFormat("en-US", { timeZone: ET, hour: "numeric", minute: "2-digit", hour12: true })
    .format(d)
    .replace(/\u202f/g, " ");
  return `${day} at ${time} ET`;
}

function etDay(iso: string): string {
  return new Intl.DateTimeFormat("en-US", { timeZone: ET, month: "long", day: "numeric" }).format(new Date(iso));
}

// NO pot mention anywhere on this page — the pot is admin-only.
const rules = (info: LaunchInfo): string[] => [
  // Every account is a workspace, and `POST /launch/join` refuses workspaces,
  // so entry is the team hand-raise below (the founders add each one).
  "Sign up, then raise your hand as a team below — the founders enter you by hand.",
  `The sheet closes ${etDateTime(info.sheet_closes_at)}.`,
  `The first ${info.comp_slots.toLocaleString("en-US")} on the sheet get their first month free.`,
  `Every referral who subscribes moves you up ${info.referral_jump} spots — the first 20 count.`,
  "Referral credits: 5 subscribed referrals earn $50 in credits; 20 earn $250 total.",
  `The competition runs ${etDay(info.competition_start_at)} – ${etDay(info.competition_end_at)}: connect a read-only Stripe key, and the board ranks the dollar lift over your prior 30 days.`,
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
            {info.phase === "open" && !isSignedIn ? (
              <div className="lc-actions" style={{ justifyContent: "center" }}>
                <a href={`${SITE_URL}/sign-up`} className="ld-btn">Sign up <span className="ld-btn-arrow" aria-hidden="true">→</span></a>
                <p className="lc-fine" style={{ flexBasis: "100%", textAlign: "center" }}>
                  Already signed up? <a href={`${SITE_URL}/sign-in`}>Sign in</a>
                </p>
              </div>
            ) : null}
          </LaunchHero>

          {info.phase === "competition" || info.phase === "ended" ? (
            <section className="ld-section" id="board">
              <p className="ld-kicker">The board</p>
              <h2 className="ld-h2">Revenue lift, <em>live</em>.</h2>
              <Leaderboard />
            </section>
          ) : null}

          {isSignedIn ? (
            <section className="ld-section" id="you">
              <SheetCard info={info} />
            </section>
          ) : null}

          {/* Teams do not self-serve — this raises a hand, the founders do the
              entry. Deliberately subordinate to the individual path above, and
              only while the sheet is open (there is nothing to enter after). */}
          {info.phase === "open" ? (
            <section className="ld-section" id="teams">
              <TeamInterestForm signedIn={!!isSignedIn} />
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
