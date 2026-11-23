"use client";

import { PHONE_VOICES, voiceSamplePath } from "@lightbulb/shared";

/**
 * The homepage's middle: what the assistant does on a call, the voices, what
 * the owner sees afterwards, and the controls the owner keeps. Every line maps
 * to a VERIFIED row in .agents/product-marketing.md; check there before adding one.
 */

const ON_THE_CALL: [string, string][] = [
  ["Answers, day or night", "After closing and on your day off too. It handles several calls at once."],
  [
    "Answers questions",
    "Hours, services, prices, staff, directions: only what you told it. When it doesn’t know, it says it will check and takes a message.",
  ],
  [
    "Books the appointment",
    "During the call, inside your opening hours, with the right person and your buffer between clients. Into your Google Calendar or its own calendar.",
  ],
  [
    "Moves and cancels",
    "For your clients, when their phone carrier verifies their number. Otherwise it takes a message for you.",
  ],
  ["Takes a message", "Name, callback number and what they need, waiting for you in Needs you."],
  [
    "Hands the call to you",
    "When a caller wants a person, it transfers them to your mobile, only during opening hours if you prefer.",
  ],
];

export function OnTheCall() {
  return (
    <section className="lb-sec" id="on-the-call">
      <div className="lb-wrap">
        <h2 className="lb-h2">What your assistant does on a call</h2>
        <ul className="lb-does">
          {ON_THE_CALL.map(([title, body]) => (
            <li key={title}>
              <b>{title}</b>
              {body}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

/** The stock samples in /public/voice-samples. They are not a personalized greeting, and the copy says so. */
export function Voices() {
  return (
    <section className="lb-sec deep" id="voices">
      <div className="lb-wrap">
        <h2 className="lb-h2">Hear the voices</h2>
        <p className="lb-sec-sub">
          Pick one of three. These are stock samples; on your line it uses the name and greeting you choose. Switch
          on Spanish and it follows the caller.
        </p>
        <div className="lb-voices">
          {PHONE_VOICES.map((v) => (
            <div key={v.id} className="lb-voice">
              <h3>{v.label}</h3>
              {(["en", "es"] as const).map((lang) => {
                const label = `${v.label.replace(" (default)", "")}, ${lang === "en" ? "English" : "Spanish"}`;
                return (
                  <figure key={lang}>
                    <figcaption>{lang === "en" ? "English" : "Spanish"}</figcaption>
                    <audio controls preload="none" src={voiceSamplePath(v.id, lang)} aria-label={`${label} sample`} />
                  </figure>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

export function AfterTheCall() {
  return (
    <section className="lb-sec" id="after">
      <div className="lb-wrap lb-split">
        <div>
          <h2 className="lb-h2">After every call, you see exactly what happened.</h2>
          <ul className="lb-plain">
            <li>Every call on your Home: who called, when, how it ended, and a one-line summary.</li>
            <li>Open a call to see what your assistant did, step by step, and the full transcript.</li>
            <li>
              Anything it couldn&rsquo;t decide goes to <b>Needs you</b>. Answer once and it knows next time.
            </li>
            <li>Want it to say something differently? Write it in &ldquo;Next time&hellip;&rdquo; on that call.</li>
          </ul>
        </div>

        {/* Decorative: a copy of a real Needs you card (voice/NeedsYou.tsx). */}
        <div className="lb-needs" aria-hidden="true">
          <span className="lb-tag warn">Needs you</span>
          <p className="lb-needs-t">Nova didn&rsquo;t know</p>
          <p className="lb-needs-q">&ldquo;Do you do bridal parties?&rdquo; Mia, 6:40 pm</p>
          <div className="lb-compose-in">Yes, up to six people. Book a consult first.</div>
          <div className="lb-ask-r">
            <span className="y">Answer</span>
            <span>Call back</span>
          </div>
        </div>
      </div>
    </section>
  );
}

const IN_CHARGE: [string, string][] = [
  [
    "It never pretends to be a person",
    "The default greeting introduces it as your AI assistant. If a caller sincerely asks whether it’s a person, it tells the truth.",
  ],
  [
    "Your greeting is yours",
    "Some states require telling callers they’re talking to an AI, or that calls are recorded. You write the greeting, so that part is your call, and the app reminds you.",
  ],
  [
    "It only does what you allow",
    "Booking, moving and cancelling, greeting regulars by name, answering questions: switch any of them off and it takes a message instead.",
  ],
  ["No payments, no card numbers", "It doesn’t take payments, and it removes card numbers from every record."],
  ["Clients only reach their own bookings", "It won’t share anything about your other clients."],
  ["Pause it anytime", "A paused line only takes messages."],
];

export function InCharge() {
  return (
    <section className="lb-sec deep" id="in-charge">
      <div className="lb-wrap">
        <h2 className="lb-h2">You stay in charge</h2>
        <ul className="lb-does">
          {IN_CHARGE.map(([title, body]) => (
            <li key={title}>
              <b>{title}</b>
              {body}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
