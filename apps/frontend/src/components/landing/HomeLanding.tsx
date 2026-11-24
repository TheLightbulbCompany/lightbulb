import { Bricolage_Grotesque, Kalam } from "next/font/google";
import Link from "next/link";

import { Footer } from "./Footer";
import { FounderLink, HomePlayer, PhoneSignupLink, SparkDownloadLink } from "./HomePlayer";
import { Navbar } from "./Navbar";
import { ASKS, CALLS, STICKY, TILES } from "./homeDemo";
import "./home.css";

const hand = Kalam({ subsets: ["latin"], weight: "400", variable: "--lh-hand", display: "swap" });
const spark = Bricolage_Grotesque({ subsets: ["latin"], weight: ["500", "600", "700"], variable: "--lh-spark", display: "swap" });

/*
 * The company home, ported from the owner-approved mockup (2026-11-24, with his two changes: Lightbulb's cream
 * palette and Spark first). One line whose last word turns (life, business, computer), then a working window
 * with one tab per product, then one row per product. The server HTML carries every word, button and link,
 * drawn at its resting state; HomePlayer adds the turning word, the tabs and the play buttons.
 */

const dana = CALLS[0];

function Book() {
  return (
    <div className="book" aria-label="Appointment book">
      <h3>
        <span id="bDay">{dana.book.day}</span>
        <small id="bChair">{dana.book.chair}</small>
      </h3>
      <div className="rows" id="rows">
        {dana.book.rows.map(([t, w]) => {
          const hit = t === dana.book.at;
          return (
            <div className={`row${hit ? " hit" : ""}`} key={t}>
              <time>{t}</time>
              <span className={`w${hit ? " new done" : ""}`}>{hit ? dana.book.text : w}</span>
            </div>
          );
        })}
      </div>
      <div className="sticky hide" id="sticky">
        <b>Needs you</b>
        {STICKY}
      </div>
    </div>
  );
}

function PhonePanel() {
  return (
    <div className="phn">
      <div className="calls" id="calls">
        <p className="colhead">Today&apos;s calls</p>
        {CALLS.map((c, i) => (
          <button type="button" className="call" aria-pressed={i === 0} key={c.who}>
            <b>{c.who}</b>
            <time>{c.time}</time>
            <span>
              <i className={`chip ${c.chip[0]}`}>{c.chip[1]}</i>
              {c.sum}
            </span>
          </button>
        ))}
      </div>
      <div className="convo">
        <div className="display">
          <div className="clock">
            <b id="dClock">{dana.time}</b>
            <small id="dDay">{dana.day}</small>
          </div>
          <div className="state">
            <span className="livedot" id="dDot" />
            <div>
              <small id="dState">Call ended</small>
              <b id="dWho">{dana.who}</b>
            </div>
            <span className="timer" id="dTimer">
              {dana.len}
            </span>
          </div>
        </div>
        <div className="lines" id="lines" aria-live="polite">
          {dana.lines.map(([w, t], i) => (
            <div className={`ln${w === "Nova" ? " nova" : ""}`} key={i}>
              <b>
                <span className="livedot" />
                {w}
              </b>
              <span>{t}</span>
            </div>
          ))}
        </div>
        <div className="did" id="did">
          <p>What Nova did</p>
          <ul>
            {dana.did.map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        </div>
        <div className="play">
          <button type="button" className="btn" id="playCall">
            Play this call
          </button>
        </div>
      </div>
      <Book />
    </div>
  );
}

function MeetingsPanel() {
  const nothing = <li className="empty">Nothing yet</li>;
  return (
    <div className="mt">
      <div className="stage">
        <div className="call-frame" aria-label="Google Meet call">
          {TILES.map(([id, name, c]) => (
            <div className="tile" id={`m-${id}`} key={id} style={{ "--c": c } as React.CSSProperties}>
              <span className="av">{name[0]}</span>
              <span className="nm">{name}</span>
              <span className="bars" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
            </div>
          ))}
          <div className="tile nova" id="m-nova">
            <span className="av" aria-hidden="true" />
            <span className="nm">Nova</span>
            <span className="bars" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
          </div>
        </div>
        <div className="capbar" id="capbar">
          <span className="orb" aria-hidden="true" />
          <p id="cap" aria-live="polite">
            <span className="idle">Nova is listening for its name.</span>
          </p>
        </div>
      </div>
      <div className="notes">
        <p className="title">
          Monday team call<small>Notes fill in as people talk</small>
        </p>
        <div className="nsec">
          <h4>Notes</h4>
          <ul id="nNotes">{nothing}</ul>
        </div>
        <div className="nsec">
          <h4>Decisions</h4>
          <ul id="nDec">{nothing}</ul>
        </div>
        <div className="nsec">
          <h4>Follow-ups</h4>
          <ul id="nFol">{nothing}</ul>
        </div>
        <div className="play">
          <button type="button" className="btn" id="playMeet">
            Play the meeting
          </button>
        </div>
      </div>
    </div>
  );
}

const PHOTOS = ["p1", "p2", "p3", "p4", "p5", "p6"];

function SparkPanel() {
  return (
    <div className="sk" id="sk">
      <canvas className="sky" id="sky" aria-hidden="true" />
      <div className="notch" id="notch">
        <canvas id="crea" width={56} height={52} aria-hidden="true" />
        <div className="ncap" aria-live="polite">
          <small id="nLab">Listening</small>
          <span id="nTxt">Say what you want done.</span>
        </div>
      </div>
      <div className="asks">
        <p>Click one. Spark hears it and does the clicking.</p>
        {ASKS.map(([k, said]) => (
          <button type="button" className="ask" data-k={k} aria-pressed="false" key={k}>
            &ldquo;{said}&rdquo;
          </button>
        ))}
      </div>
      <div className="desk" aria-hidden="true">
        <div className="mw">
          <div className="hd">
            <span className="av">S</span>
            <div>
              <b>Sam</b>
              <small>Active now</small>
            </div>
          </div>
          <div className="chat" id="chat">
            <span className="day">Today 4:12 pm</span>
            <span className="bub">are you still at the beach??</span>
            <span className="bub">send pics 🙏</span>
          </div>
          <div className="strip" id="strip">
            {PHOTOS.map((p) => (
              <span className={`ph ${p}`} key={p} />
            ))}
          </div>
          <div className="inrow">
            <span className="inp">Message</span>
            <span className="send" id="send">
              <svg viewBox="0 0 16 16">
                <path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" />
              </svg>
            </span>
          </div>
        </div>
        <div className="mw fw">
          <div className="hd">
            <span className="ic">
              <svg width="16" height="14" viewBox="0 0 16 14">
                <path d="M1 3h5l1.5 1.5H15V13H1Z" />
              </svg>
            </span>
            <div>
              <b>Downloads</b>
              <small id="fCount">3 items</small>
            </div>
          </div>
          <div className="files" id="files">
            {["March", "Hotel", "Taxi"].map((f) => (
              <span className="file" key={f}>
                <i />
                {f}.pdf
              </span>
            ))}
            <span className="folder" id="folder">
              <i />
              <span>
                <b>Receipts</b>
                <small id="fIn">Empty</small>
              </span>
            </span>
          </div>
        </div>
      </div>
      <div className="pointer" id="pointer" aria-hidden="true">
        <svg width="26" height="34" viewBox="0 0 26 36">
          <path d="M2 2 L2 29 L9.5 22.5 L14.5 34 L19.5 31.8 L14.6 20.6 L24.5 20.2 Z" />
        </svg>
      </div>
    </div>
  );
}

// Spark first (owner, 2026-11-24); the headline keeps his order: life, business, computer.
const TABS = [
  { id: "spark", label: "Spark", Panel: SparkPanel },
  { id: "phone", label: "Phone", Panel: PhonePanel },
  { id: "meetings", label: "Meetings", Panel: MeetingsPanel },
];

function Still({ href, src, alt }: { href: string; src: string; alt: string }) {
  return (
    <Link className="still" href={href} tabIndex={-1}>
      {/* eslint-disable-next-line @next/next/no-img-element -- a small fixed still, already sized and compressed */}
      <img src={src} width={1200} height={750} alt={alt} loading="lazy" decoding="async" />
    </Link>
  );
}

export function HomeLanding() {
  return (
    <main className={`ld-page lh ${hand.variable} ${spark.variable}`}>
      <Navbar />
      <HomePlayer />
      <section className="hero" aria-labelledby="home-h1">
        <div className="wrap">
          <h1 id="home-h1">
            <span className="sr">Voice to run your life, business and computer</span>
            <span aria-hidden="true">
              Voice to run your{" "}
              <span className="word" id="word">
                life
              </span>
            </span>
          </h1>
          <p className="sub">Say it like you&apos;d say it to a person.</p>

          <div className="win" id="win">
            <div className="chrome">
              <span className="lights" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              <div className="tabs" role="tablist" aria-label="Try a product">
                {TABS.map((t, i) => (
                  <button
                    type="button"
                    className="tab"
                    role="tab"
                    id={`t-${t.id}`}
                    aria-controls={`p-${t.id}`}
                    aria-selected={i === 0}
                    tabIndex={i === 0 ? 0 : -1}
                    key={t.id}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              <span className="ex">Examples, not real customers</span>
            </div>
            {TABS.map(({ id, Panel }, i) => (
              <div
                className="panel"
                id={`p-${id}`}
                role="tabpanel"
                aria-labelledby={`t-${id}`}
                tabIndex={0}
                hidden={i !== 0}
                key={id}
              >
                <Panel />
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="products" aria-label="Products">
        <div className="wrap">
          <article className="prow">
            <div>
              <h2>Spark</h2>
              <p className="line">One-click voice interface for your Mac.</p>
              <p className="say">
                Hold fn, say it, and Spark does it. For Mac with Apple silicon (M1 or later) on macOS 14 or later.
              </p>
              <SparkDownloadLink />
            </div>
            <Still
              href="/os"
              src="/landing/home/still-os.webp"
              alt="The Spark page: a night sky, Spark's creature in the notch and a chat with Sam"
            />
          </article>
          <article className="prow">
            <div>
              <h2>Lightbulb Phone</h2>
              <p className="line">Your phone, answered and booked while your hands are busy.</p>
              <p className="say">
                For salons, spas and barbershops. It answers day and night, books your clients, takes messages and
                brings you anything it can&apos;t decide.
              </p>
              <PhoneSignupLink />
            </div>
            <Still
              href="/phone"
              src="/landing/home/still-phone.webp"
              alt="The Lightbulb Phone page: a salon's call display, a ringing phone and a handwritten Saturday book"
            />
          </article>
          <article className="prow">
            <div>
              <h2>Lightbulb Meetings</h2>
              <p className="line">Ask your meeting anything. Out loud.</p>
              <p className="say">
                Invite your assistant to a Google Meet. It stays quiet until someone says its name, then leaves notes,
                decisions and follow-ups.
              </p>
              <Link className="btn" href="/meetings#waitlist">
                Join the waitlist
              </Link>
            </div>
            <Still
              href="/meetings"
              src="/landing/home/still-meetings.webp"
              alt="The Lightbulb Meetings page: a call grid of five people and Nova, with a caption bar"
            />
          </article>
        </div>
      </section>

      <section className="close" aria-labelledby="close-h">
        <div className="wrap">
          <h2 id="close-h">Not sure which fits?</h2>
          <FounderLink />
        </div>
      </section>
      <Footer cta={false} />
    </main>
  );
}
