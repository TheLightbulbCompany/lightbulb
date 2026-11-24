import { Kalam, Schibsted_Grotesk } from "next/font/google";

import { HOME_FAQ } from "@/lib/homeFaq";
import { voicePlanLine, type Pricing } from "@/lib/pricing";

import { Footer } from "./Footer";
import { Navbar } from "./Navbar";
import { SignupLink, VoiceSamples } from "./PhoneCta";
import { PhonePlayer } from "./PhonePlayer";
import "./phone.css";

const body = Schibsted_Grotesk({ subsets: ["latin"], weight: ["400", "500", "600", "700"], variable: "--lp-font", display: "swap" });
const hand = Kalam({ subsets: ["latin"], weight: "400", variable: "--lp-hand", display: "swap" });

/*
 * Lightbulb Phone's page, ported from the owner-approved mockup (2026-11-24, v2): one day at a salon front
 * desk, cream morning to midnight, with "the line" (PhonePlayer) under the menu showing the clock, who is
 * speaking and live captions. Every sentence is a VERIFIED row in .agents/product-marketing.md ("Phone");
 * Nova is an example name and the page says so. The demo windows are pictures (aria-hidden); the words,
 * the buttons and the stock voice samples are real and work without JavaScript.
 */

const PHONE_ICON =
  "M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.2.2 2.4.6 3.6.1.3 0 .7-.2 1z";
const ANSWER = "Yes, for groups up to six, on Saturdays.";
const BOOK: [string, string][] = [
  ["9:00", "Mia, color"],
  ["9:30", ""],
  ["10:00", ""],
  ["10:30", ""],
  ["11:00", "Ray, beard trim"],
  ["11:30", ""],
  ["12:00", "Jo, blowout"],
  ["12:30", ""],
  ["1:00", "Lunch"],
];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const HOURS = ["10", "11", "12", "1", "2", "3", "4"];
const SPANISH: [string, string][] = [
  ["Nova", "Hi, I'm Nova. How can I help?"],
  ["Caller", "Hola, ¿tienen cita para uñas el viernes?"],
  ["Nova", "Sí. El viernes a las 3:00 con Ana. ¿Le va bien?"],
  ["Caller", "Perfecto, gracias."],
  ["Nova", "Listo. Viernes a las 3:00 con Ana."],
];
const CALLS: [string, string, string, string][] = [
  ["8:10 pm", "ok", "Booked", "Lucía booked nails with Ana, Friday at 3:00."],
  ["7:48 pm", "", "Answered", "Wanted a person. Handed the call to you."],
  ["7:31 pm", "warn", "Needs you", "Asked about a bridal party. Took a message."],
  ["7:02 pm", "ok", "Moved", "Moved Priya's color to Thursday at 2:00."],
  ["10:40 am", "ok", "Booked", "Dana booked a haircut with Sam, Saturday at 10:00."],
];
const DETAIL: { v: number; title: string; sum: string; did: string[]; said: [string, string][] }[] = [
  {
    v: 4,
    title: "Dana, 10:40 am",
    sum: "Booked a haircut with Sam, Saturday at 10:00.",
    did: ["Checked Saturday morning openings", "Booked a haircut with Sam, Saturday 10:00"],
    said: [
      ["Nova", "Hi, I'm Nova, the salon's AI assistant. How can I help?"],
      ["Dana", "Do you have anything Saturday morning for a haircut?"],
      ["Nova", "Sam has 10:00 or 11:30. Which works for you?"],
      ["Dana", "10, please."],
    ],
  },
  {
    v: 3,
    title: "Priya, 7:02 pm",
    sum: "Moved her color from Tuesday to Thursday at 2:00.",
    did: ["Found Priya's booking", "Checked Thursday openings", "Moved the color to Thursday 2:00"],
    said: [
      ["Priya", "Hi, it's Priya. Can I move my color from Tuesday?"],
      ["Nova", "Thursday at 2:00 is open. Does that work?"],
      ["Priya", "Perfect."],
    ],
  },
  {
    v: 2,
    title: "Elena, 7:31 pm",
    sum: "Asked about a bridal party. Took a message.",
    did: [
      "Didn't know the answer, so it said it would check",
      "Took Elena's name and callback number",
      "Put the question in Needs you",
    ],
    said: [
      ["Elena", "Do you do bridal parties?"],
      ["Nova", "I'll check with the owner. Can I take your name and number?"],
    ],
  },
];

function Play() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M4 2.5v11l9-5.5z" fill="currentColor" />
    </svg>
  );
}

function Doors({ ctaId, surface }: { ctaId: string; surface: string }) {
  return (
    <div className="acts">
      <SignupLink className="btn p" ctaId={ctaId} surface={surface} />
      <a className="btn s" href="#voices">
        <Play />
        Hear the voices
      </a>
    </div>
  );
}

function Pin({ id, height, title, children }: { id: string; height: number; title: string; children: React.ReactNode }) {
  return (
    <section id={id} className="pin dk" style={{ height: `${height}vh` }} aria-labelledby={`${id}-h`}>
      <div className="stick">
        <h2 className="say" id={`${id}-h`}>
          {title}
        </h2>
        <div className="vis">{children}</div>
      </div>
    </section>
  );
}

export function PhoneLanding({ pricing }: { pricing: Pricing | null }) {
  return (
    <main className={`ld-page lp ${body.variable} ${hand.variable}`}>
      <Navbar />
      <PhonePlayer />
      <div id="wall" aria-hidden="true" />
      <div id="sun" aria-hidden="true">
        <i />
        <i />
        <i />
      </div>
      <div id="lamp" aria-hidden="true" />
      <div id="desk" className="counter" aria-hidden="true" />

      <div id="line" aria-hidden="true">
        <div className="clk">
          <span id="clk">10:40 am</span>
          <small id="day">Monday</small>
        </div>
        <div className="bd">
          <div className="who" id="who">
            <span className="dot" id="dot" />
            <span id="wtx">Line open</span>
          </div>
          <div className="cap" id="cap">
            &nbsp;
          </div>
        </div>
        <div className="wv" id="wv">
          <b />
          <b />
          <b />
          <b />
          <b />
        </div>
      </div>
      <div className="ghost" id="ghost" aria-hidden="true" />

      {/* 1 · morning, hands in color */}
      <section id="s1" className="lt">
        <div className="s1in">
          <div className="s1t">
            <h1>Your phone, answered and booked while your hands are busy.</h1>
            <p className="lede">
              An assistant for your salon, spa or barbershop. It answers day and night, books your clients, and brings
              you anything it can&apos;t decide. Name it Nova, or anything you like.
            </p>
            <Doors ctaId="hero_start_free" surface="hero" />
            <ul className="facts">
              <li>Keep your number</li>
              <li>Month to month</li>
              <li>English and Spanish</li>
            </ul>
          </div>
          <div className="desk" aria-hidden="true">
            <div className="phone" id="ph1">
              <div className="scr">
                <span className="t1" id="ph1a">
                  Salon line
                </span>
                <span className="t2" id="ph1b">
                  10:40
                </span>
                <span className="halo" id="h1a" />
                <span className="halo" id="h1b" />
                <span className="av">
                  <svg viewBox="0 0 24 24">
                    <path d={PHONE_ICON} fill="#F5EFE2" />
                  </svg>
                </span>
                <span className="st" id="ph1c">
                  <b>&nbsp;</b>&nbsp;
                </span>
              </div>
            </div>
            <div className="book">
              <h3>
                Saturday <small>Sam&apos;s chair</small>
              </h3>
              <div className="rows">
                {BOOK.map(([t, w]) => (
                  <div className="row" key={t}>
                    <span>{t}</span>
                    {t === "10:00" ? (
                      <span>
                        <span className="slot" id="slot1">
                          <span className="tg ok">Booked</span>Dana, haircut with Sam
                        </span>
                      </span>
                    ) : (
                      <span className="w">{w}</span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
        <div className="counter" aria-hidden="true" />
      </section>

      {/* 2 · 7 pm, closed */}
      <Pin id="s2" height={260} title="7 pm. You're closed, and Nova still answers.">
        <div className="sign" aria-hidden="true">
          <div className="str" />
          <div className="card" id="sign">
            <div className="face f1">Open</div>
            <div className="face f2">Closed</div>
          </div>
        </div>
        <div className="app week" aria-hidden="true">
          <div className="hd">
            <span>This week</span>
            <span className="tg" id="mvtag">
              Calendar
            </span>
          </div>
          <div className="wk" id="wk">
            <span />
            {DAYS.map((d) => (
              <span className="dn" key={d}>
                {d}
              </span>
            ))}
            {HOURS.flatMap((h) => [
              <span className="hr" key={h}>
                {h}:00
              </span>,
              ...DAYS.map((d) => <span className="cell" key={h + d} />),
            ])}
          </div>
        </div>
      </Pin>

      {/* 3 · needs you */}
      <Pin id="s3" height={300} title="When Nova doesn't know, it asks you. Answer once and it knows.">
        <div className="app ny" id="ny" aria-hidden="true">
          <div className="top">
            <span className="tg warn">Needs you</span>
            <span className="mute" style={{ fontSize: 13 }}>
              7:31 pm
            </span>
          </div>
          <h4>Asked about a bridal party. Took a message.</h4>
          <p className="from mute">Elena left her name and callback number.</p>
          <span className="lbl">Next time…</span>
          <div className="box" id="ans">
            {ANSWER}
          </div>
          <div className="sv">
            <span className="done" id="saved">
              Saved. Nova knows this now.
            </span>
            <span className="pb" id="save">
              Save answer
            </span>
          </div>
        </div>
        <div className="app nx" id="nx" aria-hidden="true">
          <div className="top">
            <span className="tg" id="nxtag">
              Next call
            </span>{" "}
            <span className="mute" id="nxt" style={{ fontSize: 13, marginLeft: 8 }}>
              Later tonight
            </span>
          </div>
          <div className="ln" id="nx1">
            <b>Caller</b>Do you do bridal parties?
          </div>
          <div className="ln" id="nx2">
            <b>Nova</b>We do, for groups up to six, on Saturdays.
          </div>
        </div>
      </Pin>

      {/* 4 · hand-off */}
      <Pin id="s4" height={260} title="Want a person? Nova hands the call to your mobile.">
        <div className="ho" aria-hidden="true">
          <div className="node">
            <span className="c">
              <svg viewBox="0 0 24 24">
                <path d={PHONE_ICON} fill="#F5EFE2" />
              </svg>
            </span>
            Caller
          </div>
          <div className="wire">
            <b id="pu1" />
          </div>
          <div className="node nv">
            <span className="c">
              <svg viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="5" fill="#0F1A2B" />
              </svg>
            </span>
            Nova
          </div>
          <div className="wire">
            <b id="pu2" />
          </div>
          <div className="mob">
            <div className="scr" id="mob">
              <span className="t1" id="mob1">
                Your mobile
              </span>
              <span className="t2" id="mob2">
                7:48
              </span>
              <div className="bt" id="mobbt" style={{ opacity: 0 }}>
                <span className="no">
                  <svg viewBox="0 0 24 24">
                    <path d="M5 5l14 14M19 5 5 19" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" />
                  </svg>
                </span>
                <span className="yes" id="yes">
                  <svg viewBox="0 0 24 24">
                    <path d={PHONE_ICON} fill="#0F1A2B" />
                  </svg>
                </span>
              </div>
            </div>
          </div>
        </div>
        <div className="app hset" aria-hidden="true">
          <h5>Hand-off to a person</h5>
          <div className="num">Your mobile</div>
          <div className="chk">
            <i />
            Only during opening hours
          </div>
        </div>
      </Pin>

      {/* 5 · spanish */}
      <Pin id="s5" height={260} title="A caller speaks Spanish, and Nova follows.">
        <div className="app tx" aria-hidden="true">
          <div className="hd">
            <b>Caller, 8:10 pm</b>
            <span className="tg" id="txtag">
              On the call
            </span>
          </div>
          <h5>Transcript</h5>
          {SPANISH.map(([who, said], i) => (
            <div className="ln" key={i}>
              <b>{who}</b>
              {said}
            </div>
          ))}
        </div>
        <div className="side5" aria-hidden="true">
          <div className="app tog">
            Also speaks Spanish
            <span className="sw on" />
          </div>
          <div className="app bk" id="bk5">
            <span className="tg ok">Booked</span>
            <p>Lucía, nails with Ana, Friday at 3:00.</p>
          </div>
        </div>
      </Pin>

      {/* 6 · the home */}
      <Pin id="s6" height={240} title="Every call, with what Nova did and what was said.">
        <div className="app home" aria-hidden="true">
          <svg id="ptr" viewBox="0 0 22 26">
            <path
              d="M2 2v19l5-4.5 3.6 7.6 3.4-1.6-3.5-7.4H17z"
              fill="#0F1A2B"
              stroke="#fff"
              strokeWidth="1.6"
              strokeLinejoin="round"
            />
          </svg>
          <div className="lst">
            <h5>Home</h5>
            {CALLS.map(([tm, kind, tag, text], i) => (
              <div className={`it${i === 4 ? " sel" : ""}`} data-i={i} key={i}>
                <span className="tm">{tm}</span>
                <div>
                  <span className={`tg${kind ? ` ${kind}` : ""}`}>{tag}</span>
                  <p>{text}</p>
                </div>
              </div>
            ))}
          </div>
          <div className="pane">
            {DETAIL.map((d) => (
              <div className={`pv${d.v === 4 ? " on" : ""}`} data-v={d.v} key={d.v}>
                <h4>{d.title}</h4>
                <p className="sm">{d.sum}</p>
                <h6>What Nova did</h6>
                <ul>
                  {d.did.map((x) => (
                    <li key={x}>{x}</li>
                  ))}
                </ul>
                <h6>Transcript</h6>
                {d.said.map(([who, said], i) => (
                  <p className="q" key={i}>
                    <b>{who}</b>
                    {said}
                  </p>
                ))}
              </div>
            ))}
          </div>
        </div>
      </Pin>

      {/* 7 · setup; the voice samples are real */}
      <Pin id="s7" height={260} title="Keep your number, name your assistant, pick a voice.">
        <div className="set">
          <div className="app" aria-hidden="true">
            <h4>Your number</h4>
            <p className="sub">Nothing to port. Forward your calls with your carrier&apos;s code.</p>
            <div className="opt" id="o1">
              <i />
              Keep my number
            </div>
            <div className="opt">
              <i />
              Get a new number
            </div>
            <p className="sub" style={{ marginTop: 14 }}>
              Which calls go to Nova?
            </p>
            <div className="opt" id="o2">
              <i />
              Missed and busy calls only
            </div>
            <div className="opt">
              <i />
              All calls
            </div>
            <p className="okline" id="fw">
              Forwarding checked. It works.
            </p>
          </div>
          <div className="app" aria-hidden="true">
            <h4>Name</h4>
            <p className="sub">Not a real staff member&apos;s name.</p>
            <div className="inp" id="nm">
              Nova
            </div>
            <div>
              <span className="chip2">Lightbulb</span>
              <span className="chip2 on" id="cN">
                Nova
              </span>
              <span className="chip2">Sol</span>
              <span className="chip2">Remy</span>
            </div>
            <p className="sub" style={{ marginTop: 20 }}>
              Greeting
            </p>
            <div className="opt" style={{ display: "block", lineHeight: 1.45 }} id="gr">
              Hi, I&apos;m Nova, the salon&apos;s AI assistant. How can I help?
            </div>
          </div>
          <div className="app" id="voices">
            <h3 className="h4">Voice</h3>
            <p className="sub">Three voices. These are stock samples.</p>
            <VoiceSamples />
            <div className="opt" style={{ justifyContent: "space-between", marginTop: 18 }} aria-hidden="true">
              Also speaks Spanish
              <span className="sw" id="sp7" />
            </div>
          </div>
        </div>
      </Pin>

      {/* 8 · questions (still); the same words feed the FAQPage structured data */}
      <section id="faq" className="still dk" aria-labelledby="faq-h">
        <h2 className="say" id="faq-h">
          Questions.
        </h2>
        <div className="faq">
          {HOME_FAQ.map(({ q, a }) => (
            <details open key={q}>
              <summary>{q}</summary>
              <p>{a}</p>
            </details>
          ))}
        </div>
      </section>

      {/* 9 · closing (still) */}
      <section id="s9" className="still dk" aria-labelledby="s9-h">
        <h2 id="s9-h">Let your phone get answered while you work.</h2>
        <Doors ctaId="closing_start_free" surface="phone_closing" />
        <p className="small">
          {voicePlanLine(pricing) ?? "One monthly plan with minutes included."} Month to month, cancel anytime. The
          voices are stock samples.
        </p>
        <div className="counter" aria-hidden="true" />
      </section>
      <Footer cta={false} />
    </main>
  );
}
