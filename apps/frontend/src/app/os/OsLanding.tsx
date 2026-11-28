import { Bricolage_Grotesque, Silkscreen } from "next/font/google";
import Link from "next/link";
import { Suspense } from "react";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import "@/components/landing/spark.css";

import { DownloadBlock, OsInvite, SOON } from "./OsInvite";
import { SparkGet } from "./SparkDownload";
import { SparkPlayer } from "./SparkPlayer";

export type SparkPrice = { priceText: string; trialDays: number; trialVoiceMinutes?: number } | null;

// "7 days", or "7 days or 2 hours of talking, whichever comes first" when the backend sends the trial's talking time.
export function freeFor({ trialDays, trialVoiceMinutes: m }: NonNullable<SparkPrice>): string {
  if (!m) return `${trialDays} days`;
  const talk = m % 60 ? `${m} minutes` : `${m / 60} hour${m === 60 ? "" : "s"}`;
  return `${trialDays} days or ${talk} of talking, whichever comes first`;
}

const body = Bricolage_Grotesque({ subsets: ["latin"], variable: "--sp-font", display: "swap" });
const pixel = Silkscreen({ weight: "400", subsets: ["latin"], variable: "--sp-pixel", display: "swap" });

// Every sentence on the page. Facts (price, trial, Mac, privacy) follow .agents/product-marketing.md ("Spark").
const SYSTEM = "For Mac with Apple silicon (M1 or later). Requires macOS 14 or later.";
const FRIEND = "Each friend who joins with your link adds a free day.";
const H1_A = "Talk to your Mac.";
const H1_B = "Spark does the clicking.";
const S2 = "One sentence instead of all the clicking.";
const S2_DONE = "Three photos sent to Sam.";
const S3 = "Hold fn, say it, and Spark does it.";
const S4 = "It works in the apps you already use.";
const S5 = "Spark thinks with a plan you already have.";
const S5_SUB = "Runs on your own ChatGPT or Claude.";
const S6 = "Where your things go.";
const S7 = "Questions.";
const PRIV_VOICE = "Your voice goes to OpenAI so Spark can understand and answer you. We don't store the audio.";
const PRIV_SEES = "What Spark sees and does on your Mac goes to your own ChatGPT or Claude account, not to us.";
const PRIV_FILES = "Your files and history";
const PRIV_STAY = "stay on your Mac.";
const VOICE_ANSWER =
  "No. Your voice is processed by OpenAI to understand and answer you, and we do not store the audio. We keep a text transcript for 90 days to improve Spark.";
const BY_HAND = [
  ["Open the chat", "0:06"],
  ["Find Sam", "0:14"],
  ["Open your photos", "0:25"],
  ["Pick the three from today", "0:48"],
  ["Attach and type a note", "1:05"],
  ["Send", "1:12"],
] as const;
const HOW = [
  ["Hold fn", "Bottom left of your keyboard."],
  ["Say it", "Talk the way you'd ask a friend."],
  ["Spark does it", "You watch it click."],
] as const;
const CAN = [
  ["See your screen", "It looks at what you're looking at."],
  ["Circle anything", "Say what you mean and it points."],
  ["Keep listening", "Leave it on and keep talking."],
  ["Stop any time", "Say stop and Spark stops."],
] as const;
const SETUP = [
  ["Account", "Make your Spark account."],
  ["Brain", "Pick the AI you already use."],
  ["Mac", "Let Spark see the screen and click."],
  ["Hi", "Say hi to check it hears you."],
] as const;

// Spark's landing page: eight scenes; in the first six Spark's pointer acts on the page (SparkPlayer drives them
// from the scroll position; the demo windows are pictures, hidden from screen readers). Every price and
// trial length comes from GET /os/config (`price`); without it the sentences that carry numbers are
// omitted or said without them.
export function OsLanding({ ready, price }: { ready: boolean; price: SparkPrice }) {
  const faq: [string, string][] = [
    ["Is Spark free?", price ? `For ${freeFor(price)}. Then it's ${price.priceText}.` : "See the price when you download."],
    ["What Mac do I need?", "A Mac with Apple silicon (M1 or later), on macOS 14 or later."],
    ["Do I need ChatGPT or Claude?", "Yes, your own account. Free ChatGPT works. Claude needs Pro or higher."],
    ["Can Spark do things without asking?", "It acts on your screen when you ask. Say stop any time and it stops."],
    ["Is my voice stored?", VOICE_ANSWER],
    ["How do I invite friends?", `Share your link from the app. ${FRIEND}`],
  ];

  return (
    <main className={`ld-page sp ${body.variable} ${pixel.variable}`}>
      <Navbar />
      <SparkPlayer />

      <section id="s1" className="sp-hero">
        <canvas className="sky" id="sky1" aria-hidden="true" />
        <div className="s1in">
          <h1 className="line1">
            <span>{H1_A}</span> <span className="dim">{H1_B}</span>
          </h1>
          <div className="act" id="get">
            <Suspense fallback={<DownloadBlock ready={ready} source="os_hero" />}>
              <OsInvite ready={ready} source="os_hero" />
            </Suspense>
            <div className="small">
              {price && (
                <p>
                  Free for {freeFor(price)}, then {price.priceText}. Renews monthly until you cancel.
                </p>
              )}
              <p className="legal">
                <Link href="/terms">Terms</Link>
                <Link href="/privacy">Privacy</Link>
              </p>
              <p>{SYSTEM}</p>
            </div>
          </div>
        </div>
        <div className="chat" aria-hidden="true">
          <div className="ch-top">
            <span className="av">S</span>
            <div>
              <b>Sam</b>
              <small>Active now</small>
            </div>
          </div>
          <div className="ch-thread">
            <div className="day">Today 4:12 pm</div>
            <div className="msg in">are you still at the beach??</div>
            <div className="msg in">send pics 🙏</div>
            <div className="pics" id="pics">
              <i className="slot ph p1" />
              <i className="slot ph p3" />
              <i className="slot ph p4" />
            </div>
            <div className="msg out" id="outtxt">
              From today 🌊
            </div>
            <div className="deliv" id="deliv">
              Delivered
            </div>
          </div>
          <div className="ch-strip" id="strip">
            {[1, 2, 3, 4, 5, 6].map((n) => (
              <i key={n} className={`ph p${n}`} />
            ))}
          </div>
          <div className="ch-input">
            <span className="inp" id="inp" />
            <span className="send" id="send">
              <svg viewBox="0 0 16 16">
                <path d="M8 14V3M3.5 7.5 8 3l4.5 4.5" />
              </svg>
            </span>
          </div>
        </div>
      </section>

      <section id="s2" className="pin" style={{ height: "260vh" }} aria-labelledby="sp-s2">
        <div className="stick">
          <h2 className="say" id="sp-s2">
            {S2}
          </h2>
          <div className="cmp">
            <div className="win">
              <div className="cmp-h">
                <span>By hand</span>
                <span className="clock" id="hclock" aria-hidden="true">
                  0:00
                </span>
              </div>
              <ol className="steps" id="steps">
                {BY_HAND.map(([step, time]) => (
                  <li key={step}>
                    <i className="bx" />
                    <span className="mk st">{step}</span>
                    <span className="tm">{time}</span>
                  </li>
                ))}
              </ol>
            </div>
            <div className="sp-side">
              <div className="cmp-h">
                <span>With Spark</span>
                <span className="clock" id="sclock" aria-hidden="true">
                  0:00
                </span>
              </div>
              <p className="spoken" id="spoken" aria-hidden="true" />
              <div className="done" id="done">
                <b>Done.</b>
                <span>{S2_DONE}</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="s3" className="pin" style={{ height: "240vh" }} aria-labelledby="sp-s3">
        <div className="stick">
          <h2 className="say" id="sp-s3">
            {S3}
          </h2>
          <ol className="how">
            <li className="hw">
              <div className="hw-vis" aria-hidden="true">
                <div className="kb">
                  <span className="key ghosted wide">
                    <span className="w">shift</span>
                  </span>
                  <span className="key ghosted">
                    <span className="t">Z</span>
                  </span>
                  <span className="key ghosted">
                    <span className="t">X</span>
                  </span>
                  <span className="key fn" id="fnkey">
                    <span className="t">fn</span>
                    <svg className="g" viewBox="0 0 18 18">
                      <circle cx="9" cy="9" r="7.5" />
                      <ellipse cx="9" cy="9" rx="3.2" ry="7.5" />
                      <path d="M1.5 9h15" />
                    </svg>
                  </span>
                  <span className="key ghosted">
                    <span className="w">control</span>
                  </span>
                  <span className="key ghosted">
                    <span className="w">option</span>
                  </span>
                </div>
              </div>
              <span className="n">1</span>
              <h3>{HOW[0][0]}</h3>
              <p>{HOW[0][1]}</p>
            </li>
            <li className="hw">
              <div className="hw-vis" aria-hidden="true">
                <div className="live">
                  <div className="lab">LISTENING</div>
                  <div className="wave" id="wave" />
                  <div className="cap" id="live" />
                </div>
              </div>
              <span className="n">2</span>
              <h3>{HOW[1][0]}</h3>
              <p>{HOW[1][1]}</p>
            </li>
            <li className="hw">
              <div className="hw-vis" aria-hidden="true">
                <div className="cal">
                  <h4>
                    Rooms · Thursday <span>Room 2</span>
                  </h4>
                  <div className="slotrow">
                    <span className="h">1 pm</span>
                    <span className="ev taken">Design review</span>
                  </div>
                  <div className="slotrow">
                    <span className="h">2 pm</span>
                    <span className="ev taken">Taken</span>
                  </div>
                  <div className="slotrow">
                    <span className="h">3 pm</span>
                    <span className="ev free" id="slot3">
                      Free
                    </span>
                  </div>
                  <div className="slotrow">
                    <span className="h">4 pm</span>
                    <span className="ev free">Free</span>
                  </div>
                </div>
              </div>
              <span className="n">3</span>
              <h3>{HOW[2][0]}</h3>
              <p>{HOW[2][1]}</p>
            </li>
          </ol>
        </div>
      </section>

      <section id="s4" className="pin" style={{ height: "380vh" }} aria-labelledby="sp-s4">
        <div className="stick dots">
          <h2 className="say" id="sp-s4">
            {S4}
          </h2>
          <div className="daw" aria-hidden="true">
            <div className="dw-top">
              <span className="play" id="play">
                <svg viewBox="0 0 14 14">
                  <path id="playIco" d="M3 1.5v11l9.5-5.5z" />
                </svg>
              </span>
              <span className="lcd">82 BPM</span>
              <span className="lcd" id="pos">
                1 . 1
              </span>
              <span className="dw-name">Untitled beat</span>
              <span className="spec" id="spec" />
            </div>
            <div className="dw-main">
              <div className="dw-heads">
                {["Drums", "Bass", "Keys"].map((t, i) => (
                  <div className="hd" key={t}>
                    <i className={`sw c${i}`} />
                    {t}
                    <span className="meter" />
                  </div>
                ))}
              </div>
              <div className="dw-grid" id="grid">
                <div className="ruler">
                  {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
                    <span key={n}>{n}</span>
                  ))}
                </div>
                {[0, 1, 2].map((i) => (
                  <div className="lane" key={i}>
                    <div className={`reg r${i}`} />
                  </div>
                ))}
                <div className="ph-head" id="phead" />
              </div>
            </div>
            <div className="dw-bin">
              <span className="binlab">LOOPS</span>
              <span className="chip c0">Dusty drums</span>
              <span className="chip off">Shaker</span>
              <span className="chip c1">Warm bass</span>
              <span className="chip off">Vinyl crackle</span>
              <span className="chip c2">Rainy keys</span>
              <span className="chip off">Strings</span>
            </div>
          </div>
          <ul className="cards">
            {CAN.map(([title, line], i) => (
              <li className="card" key={title}>
                <PixelIcon kind={i} />
                <h3>
                  {i === 1 ? (
                    <span className="mk" id="circCard">
                      {title}
                    </span>
                  ) : (
                    title
                  )}
                </h3>
                <p>{line}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section id="s5" className="pin" style={{ height: "250vh" }} aria-labelledby="sp-s5">
        <div className="stick">
          <h2 className="say" id="sp-s5">
            {S5}
          </h2>
          <p className="sub5">{S5_SUB}</p>
          <ol className="setup">
            {SETUP.map(([title, line], i) => (
              <li className="su" key={title}>
                <i className="ok" />
                <span className="n">{i + 1}</span>
                <h3>{title}</h3>
                <p>{line}</p>
                <div className="ctl" aria-hidden="true">
                  {i === 0 && (
                    <>
                      <span className="field">you@yourmail</span>
                      <span className="pbtn" id="t0">
                        Continue with email
                      </span>
                    </>
                  )}
                  {i === 1 && (
                    <span className="opts">
                      <span className="opt" id="t1">
                        ChatGPT
                      </span>
                      <span className="opt">Claude</span>
                    </span>
                  )}
                  {i === 2 && (
                    <>
                      <span className="tog">
                        Microphone
                        <span className="sw2 on" />
                      </span>
                      <span className="tog">
                        Screen and clicks
                        <span className="sw2" id="t2" />
                      </span>
                    </>
                  )}
                  {i === 3 && (
                    <>
                      <span className="hi" id="hi">
                        Hi! I&apos;m Spark.
                      </span>
                      <span className="pbtn" id="t3">
                        Say hi
                      </span>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section id="s6" className="pin" style={{ height: "200vh" }} aria-labelledby="sp-s6">
        <div className="stick dots">
          <h2 className="say" id="sp-s6">
            {S6}
          </h2>
          <ul className="priv">
            <li>
              <PixelIcon kind={4} />
              <span>{PRIV_VOICE}</span>
            </li>
            <li>
              <PixelIcon kind={5} />
              <span>{PRIV_SEES}</span>
            </li>
            <li>
              <PixelIcon kind={6} />
              <span>
                {PRIV_FILES}{" "}
                <b className="mk" id="stays">
                  {PRIV_STAY}
                </b>
              </span>
            </li>
          </ul>
          <div className="dia" aria-hidden="true">
            <div className="mac">
              <span className="tag">Your Mac</span>
              <div className="row">
                <div className="dchip scr">
                  Files and history<small>stay here</small>
                </div>
                <div className="dchip spk">
                  Spark<small>does the clicking</small>
                </div>
              </div>
            </div>
            <div className="links">
              <div className="lk">
                <b id="pl0" />
                <small>your voice</small>
              </div>
              <div className="lk">
                <b id="pl1" />
                <small>what Spark sees</small>
              </div>
            </div>
            <div className="outs">
              <div className="out">
                OpenAI<small>keeps no audio</small>
              </div>
              <div className="out">
                Your ChatGPT or Claude<small>your own account</small>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="s7" className="pin" aria-labelledby="sp-s7">
        <div className="stick">
          <h2 className="say" id="sp-s7">
            {S7}
          </h2>
          <p className="sub7">
            Something else? Hold <kbd>fn</kbd> and ask Spark.
          </p>
          <div className="faq">
            {faq.map(([q, a]) => (
              <details key={q}>
                <summary>{q}</summary>
                <p>{a}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      <section id="s8" className="pin" aria-labelledby="sp-s8">
        <div className="stick">
          <canvas className="sky" id="sky8" aria-hidden="true" />
          <div className="s8in">
            <h2 id="sp-s8">{price ? `Try Spark free for ${price.trialDays} days.` : "Try Spark."}</h2>
            <div className="act">
              <span>
                {ready ? <SparkGet source="os_closing" /> : <span className="soon">{SOON}</span>}
              </span>
              <div className="small">
                <p>
                  {price && `Then ${price.priceText}. `}
                  {FRIEND}
                </p>
                <p>
                  {price && "Renews monthly until you cancel. "}For Mac with Apple silicon (M1 or later), macOS 14 or
                  later.
                </p>
              </div>
            </div>
          </div>
        </div>
      </section>
      <Footer />
    </main>
  );
}

// Spark's pixel icons on a 12x12 grid, coloured by class (fb blue, fy yellow, fh light, fi ink) so the
// palette stays in spark.css.
const ICONS: [string, string][][] = [
  [
    ["b", "M3 3h6v1h2v1h1v2h-1v1H9v1H3V8H1V7H0V5h1V4h2z"],
    ["i", "M5 4h2v4H5z"],
    ["h", "M5 4h1v1H5z"],
  ],
  [["y", "M4 1h4v1h2v2h1v4h-1v2H8v1H4v-1H2V8H1V4h1V2h2zm0 2v1H3v4h1v1h4V8h1V4H8V3z"]],
  [["b", "M1 5h1v2H1zM3 3h1v6H3zM5 1h1v10H5zM7 4h1v4H7zM9 2h1v8H9zM11 5h1v2h-1z"]],
  [
    ["h", "M4 1h4v1h2v2h1v4h-1v2H8v1H4v-1H2V8H1V4h1V2h2z"],
    ["i", "M4 4h4v4H4z"],
  ],
  [["y", "M5 1h2v1h1v5H7v1H5V7H4V2h1zM2 6h1v2h1v1h4V8h1V6h1v2H9v1H8v1H7v1h1v1H4v-1h1v-1H4V9H3V8H2z"]],
  [
    ["b", "M1 2h10v7H1zM0 10h12v1H0z"],
    ["i", "M2 3h8v5H2z"],
  ],
  [
    ["h", "M4 1h4v1h1v3h1v6H2V5h1V2h1zm0 1v3h4V2z"],
    ["i", "M5 7h2v2H5z"],
  ],
];

function PixelIcon({ kind }: { kind: number }) {
  return (
    <svg className="px" viewBox="0 0 12 12" aria-hidden="true">
      {ICONS[kind].map(([c, d]) => (
        <path key={d} className={`f${c}`} d={d} />
      ))}
    </svg>
  );
}
