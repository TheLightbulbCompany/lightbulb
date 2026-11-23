import { Atkinson_Hyperlegible, Schibsted_Grotesk } from "next/font/google";

import { RequestSetupForm } from "@/components/requestSetup/RequestSetupForm";

import { Footer } from "./Footer";
import { MeetingsPlayer } from "./MeetingsPlayer";
import { Navbar } from "./Navbar";
import "./meetings.css";

const head = Schibsted_Grotesk({ subsets: ["latin"], weight: ["400", "500", "600", "700"], variable: "--lm-head", display: "swap" });
const body = Atkinson_Hyperlegible({ subsets: ["latin"], weight: ["400", "700"], variable: "--lm-body", display: "swap" });

/*
 * Lightbulb Meetings' page, ported from the owner-approved mockup (2026-11-24, v2): the call itself, with the
 * live caption bar at the bottom (MeetingsPlayer) and Nova's light at its left end, lit only when someone says
 * "Nova". Every sentence is a VERIFIED row in .agents/product-marketing.md ("Google Meet agent"); Nova is an
 * example name and the page says so. The demo panels are pictures (aria-hidden) drawn at their end state, so
 * the page reads without JavaScript; the words and the waitlist form are real.
 */

export const ADDRESS = "agents@your-business.thelightbulb.company";

type Who = "pri" | "dan" | "mei" | "jo" | "sam" | "nova";
const NAME: Record<Who, string> = { pri: "Priya", dan: "Dan", mei: "Mei", jo: "Jo", sam: "Sam", nova: "Nova" };

function Tile({ k, cls = "", badge = false }: { k: Who; cls?: string; badge?: boolean }) {
  if (k === "nova")
    return (
      <div className={`tile nova ${cls}`} data-k="nova">
        <div className="bulb" />
        <span className="nm">Nova</span>
      </div>
    );
  return (
    <div className={`tile ${cls}`} data-k={k} style={{ "--c": `var(--${k})` } as React.CSSProperties}>
      <span className={`badge${badge ? " on" : ""}`}>Not transcribed</span>
      <div className="av">{NAME[k][0]}</div>
      <span className="nm">{NAME[k]}</span>
      <i className="mt">
        <b />
        <b />
        <b />
      </i>
    </div>
  );
}

function Pin({ id, height, title, children }: { id: string; height: number; title: string; children: React.ReactNode }) {
  return (
    <section id={id} className="pin" style={{ height: `${height}vh` }} aria-labelledby={`${id}-h`}>
      <div className="stick">
        <div className="wrap">
          <h2 className="say" id={`${id}-h`}>
            {title}
          </h2>
          <div className="stage" aria-hidden="true">
            {children}
          </div>
        </div>
      </div>
    </section>
  );
}

const FEED: [Who, string, number?][] = [
  ["dan", "Did anyone hear back about the windows?"],
  ["mei", "Harbor Glass sent a quote on Monday."],
  ["pri", "Is it the one with the double glazing?"],
  ["mei", "Yes, fitted in two weeks."],
  ["dan", "Fine by me if it fits the budget."],
  ["sam", "It does, we checked last month."],
  ["pri", "Then let's go ahead with it."],
  ["mei", "I'll send them a yes."],
  ["pri", "Nova, add the window quote to follow-ups.", 1],
  ["nova", "Added: Mei sends the window quote to Harbor Glass.", 2],
];

const DOC: [string, [string, string?][]][] = [
  ["Notes", [["Closing on Saturdays from January."], ["Mei has the window quote from Harbor Glass."], ["Sam asked not to be transcribed."]]],
  ["Decisions", [["Close on Saturdays from January."], ["Go ahead with the new front windows."]]],
  [
    "What Nova was asked and did",
    [
      ["What did we decide about Saturdays?", "Answered in the call"],
      ["Add the window quote to follow-ups.", "Added"],
    ],
  ],
  [
    "Follow-ups",
    [["Dan tells the team about Saturdays."], ["Mei sends the window quote to Harbor Glass."], ["Priya updates the opening hours."]],
  ],
];

const ITEMS: [string, string][] = [
  ["Tell the team about Saturdays", "Dan"],
  ["Send the window quote to Harbor Glass", "Mei"],
  ["Update the opening hours", "Priya"],
];

const QUESTIONS: [string, string][] = [
  ["Which meeting apps does it work with?", "Google Meet only. Not Zoom, Microsoft Teams or Webex."],
  [
    "How does it get into my meeting?",
    "Add its address as a guest on a Google Calendar event with a Meet link. Only invites from people in your business count.",
  ],
  ["Does it talk during the call?", "Only when someone says its name. Then it answers what it was asked."],
  ["Can someone opt out?", 'Yes. It tells everyone how when it joins: "Nova, stop transcribing me."'],
  [
    "What does it leave after the call?",
    "Notes, decisions, what it was asked and did, and follow-ups. It can turn action items into follow-ups for you.",
  ],
  ["Can it spend money or send things?", "Not on its own. A spend, an email outside your business or a publish waits for your approval."],
];

export function MeetingsLanding() {
  return (
    <main className={`ld-page lm ${head.variable} ${body.variable}`}>
      <Navbar />
      <MeetingsPlayer />
      <div id="bar" aria-hidden="true">
        <div className="orb" />
        <div className="lines">
          <p className="prev" id="bp" />
          <p id="bc" />
        </div>
      </div>

      {/* 1 · the call, live */}
      <section id="s1">
        <div className="wrap">
          <div>
            <h1>Ask your meeting anything. Out loud.</h1>
            <p className="lede">Invite your assistant to the meeting. It stays quiet until someone says its name.</p>
            <a className="btn" href="#waitlist">
              Join the waitlist
            </a>
            <p className="note-s">Here it&apos;s called Nova. You pick the name.</p>
          </div>
          <div className="call" id="c1" aria-hidden="true">
            {(["pri", "dan", "mei", "jo", "nova"] as Who[]).map((k) => (
              <Tile key={k} k={k} />
            ))}
            <div className="paper">
              <h3>Decisions</h3>
              <ul>
                <li className="d on" id="d1">
                  Close on Saturdays from January.
                </li>
              </ul>
              <h3 className="h3b">Follow-ups</h3>
              <ul>
                <li className="f on" id="f1">
                  Dan tells the team.
                </li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      {/* 2 · invite it */}
      <Pin id="s2" height={240} title="Add its address to a Google Calendar invite with a Meet link, and it joins the call.">
        <div className="event">
          <div className="t">Weekly planning</div>
          <div className="row">
            <span className="k">When</span>
            <span>Thursday, 10:00 to 10:45</span>
          </div>
          <div className="row">
            <span className="k">Video</span>
            <span className="meet">Join with Google Meet</span>
          </div>
          <div className="row">
            <span className="k">Guests</span>
            <div className="guests">
              {(["pri", "dan", "mei"] as Who[]).map((k) => (
                <span className="chip" key={k}>
                  <i style={{ "--c": `var(--${k})` } as React.CSSProperties}>{NAME[k][0]}</i>
                  {NAME[k]}
                </span>
              ))}
              <span className="chip n on" id="nchip">
                <i className="ni">N</i>
                {ADDRESS}
              </span>
              <div className="field" id="gfield">
                Add guests
              </div>
            </div>
          </div>
          <div className="acts">
            <span className="btn sm saved" id="save">
              Saved
            </span>
          </div>
        </div>
        <div>
          <p className="clock" id="clk">
            Thursday, 10:00
          </p>
          <div className="mini" id="c2">
            <span className="toast" id="toast">
              Nova joined the call
            </span>
            {(["pri", "dan", "mei"] as Who[]).map((k) => (
              <Tile key={k} k={k} />
            ))}
            <Tile k="nova" cls="in" />
          </div>
        </div>
      </Pin>

      {/* 3 · it tells everyone */}
      <Pin id="s3" height={260} title="It tells everyone it's there, that the call is recorded, and how to opt out.">
        <div className="call" id="c3">
          {(["pri", "nova", "dan", "sam", "mei"] as Who[]).map((k) => (
            <Tile key={k} k={k} badge={k === "sam"} />
          ))}
        </div>
      </Pin>

      {/* 4 · quiet until named */}
      <Pin id="s4" height={280} title="It stays quiet until someone says its name.">
        <div className="feed">
          <div className="roll" id="roll">
            {FEED.map(([k, t, f], i) => (
              <p
                key={i}
                data-k={k}
                className={f === 1 ? "named" : f === 2 ? "reply on" : ""}
                style={{ "--c": k === "nova" ? "var(--bulb-ink)" : `var(--${k})` } as React.CSSProperties}
              >
                <b>{NAME[k]}</b>
                {f === 1 ? (
                  <>
                    <mark>Nova</mark>
                    {t.slice(4)}
                  </>
                ) : (
                  t
                )}
              </p>
            ))}
          </div>
        </div>
        <div className="tile nova" id="n4">
          <span className="state" id="st4">
            Quiet
          </span>
          <div className="bulb" />
          <span className="nm">Nova</span>
        </div>
      </Pin>

      {/* 5 · the notes */}
      <Pin id="s5" height={230} title="When the call ends, the notes are already there.">
        <div>
          <div className="ended">
            <span className="dots">
              {(["pri", "dan", "mei", "sam"] as Who[]).map((k) => (
                <i key={k} style={{ "--c": `var(--${k})` } as React.CSSProperties} />
              ))}
            </span>
            Weekly planning ended at 10:43
          </div>
          <div className="doc" id="doc">
            <header>
              <strong>Weekly planning</strong>
              <span>Thursday, 10:00</span>
            </header>
            {DOC.map(([h, items]) => (
              <div key={h}>
                <h3>{h}</h3>
                <ul>
                  {items.map(([t, sm]) => (
                    <li className="on" key={t}>
                      {t}
                      {sm && <small>{sm}</small>}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      </Pin>

      {/* 6 · follow-ups, and what waits for you */}
      <Pin id="s6" height={250} title="Action items become follow-ups. A spend, a send or a publish waits for you.">
        <div className="list">
          <div className="sw on" id="sw">
            <span className="tg" />
            Turn action items into follow-ups
          </div>
          {ITEMS.map(([t, w]) => (
            <div className="ai on" key={t}>
              <span className="bx" />
              {t}
              <span className="tag">Follow-up</span>
              <span className="who">{w}</span>
            </div>
          ))}
        </div>
        <div className="needs on ok" id="needs">
          <div className="h">
            <i />
            Needs you
          </div>
          <p>Nova wants to email the window quote to Harbor Glass.</p>
          <p className="sub">An email outside your business. Nothing goes until you approve it.</p>
          <div className="row">
            <span className="btn sm" id="ok6">
              Approve
            </span>
            <span className="btn sm ghost">Not now</span>
          </div>
          <div className="done">Approved by you</div>
          <span className="cur" id="cur6">
            <svg viewBox="0 0 24 24">
              <path d="M4 2l15 9-7 1.5L9 20z" strokeWidth="1.6" strokeLinejoin="round" />
            </svg>
          </span>
        </div>
      </Pin>

      {/* 7 · questions (still) */}
      <section id="s7" className="still" aria-labelledby="s7-h">
        <div className="wrap">
          <h2 id="s7-h">Questions</h2>
          <div className="qs">
            {QUESTIONS.map(([q, a], i) => (
              <details key={q} open={i === 0}>
                <summary>{q}</summary>
                <p>{a}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      {/* 8 · waitlist (still). The menu's "Join the waitlist" button lands on #waitlist. */}
      <section id="s8" className="still" aria-labelledby="s8-h">
        <div className="wrap" id="waitlist">
          <div>
            <h2 id="s8-h">Join the waitlist</h2>
            <p className="side">Leave your name and email. We&apos;ll write when Meetings is ready for you.</p>
          </div>
          <div className="wl">
            <RequestSetupForm waitlist="meetings" />
            <p className="fine">Works with Google Meet.</p>
          </div>
        </div>
      </section>

      {/* 9 · close (still) */}
      <section id="s9" aria-labelledby="s9-h">
        <div className="wrap">
          <div className="empty" aria-hidden="true">
            {(["pri", "dan", "mei", "sam"] as Who[]).map((k) => (
              <Tile key={k} k={k} cls="gone" />
            ))}
            <Tile k="nova" cls="lit" />
          </div>
          <h2 id="s9-h">Talk freely. Nova keeps the notes.</h2>
          <a className="btn" href="#waitlist">
            Join the waitlist
          </a>
        </div>
      </section>
      <Footer />
    </main>
  );
}
