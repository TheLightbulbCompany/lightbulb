"use client";

import Link from "next/link";

import { SparkStage } from "@/app/os/SparkStage";
import { captureSignupCta } from "@/lib/signupFlow";

/*
 * The home's three product sections, in the order Phone, Spark, Meetings (Meetings last: it is
 * waitlisted). Each keeps its own door: sign up for Phone, the Spark page, the Meetings waitlist.
 * No prices here; each product page carries its own.
 */

const D = "/landing/demo";

// Hovering "Download for Mac" lights the Spark bulb, as on /os (SparkStage listens for this event).
const light = (on: boolean) => window.dispatchEvent(new CustomEvent("spark:light", { detail: on }));

const Tick = () => (
  <span className="hp-tick" aria-hidden="true">
    <svg width="10" height="10" viewBox="0 0 24 24">
      <path d="M5 12l5 5 9-10" stroke="#fff" strokeWidth="3.5" fill="none" />
    </svg>
  </span>
);

export function HomeProducts() {
  return (
    <>
      <section className="hp-intro lb-wrap">
        <p>
          No apps to learn, no forms to fill.{" "}
          <span>You say what you need the way you&apos;d say it to a person, and it gets done.</span>
        </p>
      </section>

      <section className="hp-product lb-wrap" id="phone" aria-labelledby="hp-phone">
        <div className="hp-copy">
          <div className="hp-name">Lightbulb Phone</div>
          <h2 id="hp-phone">Your phone, answered and booked while your hands are busy.</h2>
          <p>
            A receptionist with the name you choose. It answers day and night, books clients into your calendar, takes
            messages and hands callers to you when it should.
          </p>
          <ul className="hp-facts">
            <li>Keep your number</li>
            <li>English and Spanish</li>
            <li>Month to month</li>
          </ul>
          <div className="hp-ctas">
            <Link
              href="/sign-up"
              className="lb-btn lb-btn-p lb-btn-xl"
              onClick={() =>
                captureSignupCta({
                  ctaId: "hub_sign_up",
                  label: "Set up Lightbulb Phone",
                  surface: "home_phone",
                  destination: "/sign-up",
                })
              }
            >
              Set up Lightbulb Phone
            </Link>
            <Link href="/phone" className="hp-more">
              See how it works
            </Link>
          </div>
        </div>
        <div className="hp-scene" aria-hidden="true">
          <div className="hp-handset">
            <div className="hp-screen">
              <div className="hp-caller">
                <b>Ava, for Bella&apos;s Salon</b>
                <small>On a call, 01:12</small>
              </div>
              <div className="hp-wave">
                {[0.1, 0.5, 0.3, 0.8, 0.2, 0.6, 0.4, 0.9, 0.15].map((d) => (
                  <i key={d} style={{ animationDelay: `-${d}s` }} />
                ))}
              </div>
              <div className="hp-snip">&ldquo;Is Maria free Saturday morning for a cut and color?&rdquo;</div>
              <div className="hp-snip ava">&ldquo;She has 11:30. Shall I book you in?&rdquo;</div>
              <div className="hp-hang" />
            </div>
          </div>
          <div className="hp-float f1">
            <b>
              <Tick />
              Booked
            </b>
            <small>Sat 11:30, cut and color with Maria</small>
          </div>
          <div className="hp-float f2">
            <b>Message for you</b>
            <small>Daniel wants to move his Tuesday to Thursday. Called at 7:05 am.</small>
          </div>
        </div>
      </section>

      <section className="hp-product hp-flip lb-wrap" id="spark" aria-labelledby="hp-spark">
        <div className="hp-copy">
          <div className="hp-name">Spark</div>
          <h2 id="hp-spark">Meet Spark</h2>
          <p className="hp-lead">One-click voice interface for your Mac.</p>
          <p>
            Hold <kbd className="hp-key">fn</kbd> and say what you want done. Spark does it on your Mac.
          </p>
          <div className="hp-ctas">
            <Link
              href="/os"
              className="lb-btn lb-btn-p lb-btn-xl"
              onMouseEnter={() => light(true)}
              onMouseLeave={() => light(false)}
              onFocus={() => light(true)}
              onBlur={() => light(false)}
            >
              Download for Mac
            </Link>
          </div>
          <p className="hp-fine">For Mac with Apple silicon (M1 or later). Requires macOS 14 or later.</p>
        </div>
        <SparkStage level="hero" className="hp-spark" label="Spark, a lightbulb with a small creature inside" />
      </section>

      <section className="hp-product lb-wrap" id="meetings" aria-labelledby="hp-meet">
        <div className="hp-copy">
          <div className="hp-name">
            Lightbulb Meetings <span className="hp-pill">Waitlist</span>
          </div>
          <h2 id="hp-meet">Leave every meeting with the notes already written.</h2>
          <p>
            Invite it to your Google Meet. It stays quiet until someone says its name, answers what it&apos;s asked, and
            leaves notes, decisions and follow-ups when you hang up.
          </p>
          <div className="hp-ctas">
            <Link href="/meetings" className="lb-btn lb-btn-p lb-btn-xl">
              Join the waitlist
            </Link>
          </div>
        </div>
        <div className="hp-scene hp-meet" aria-hidden="true">
          <div className="hp-tiles">
            {(
              [
                ["Maya", "maya"],
                ["Daniel", "daniel"],
                ["Sam", "sam"],
              ] as const
            ).map(([who, file]) => (
              <div className="hp-tile" key={who}>
                {/* eslint-disable-next-line @next/next/no-img-element -- decorative tile in a static mock-up */}
                <img src={`${D}/${file}.jpg`} alt="" />
                <small>{who}</small>
              </div>
            ))}
            <div className="hp-tile hp-lb">
              <div className="hd-orb" />
              <small>Lightbulb</small>
            </div>
          </div>
          <div className="hp-notes">
            <div className="hp-ask">&ldquo;Lightbulb, what did we decide about the launch date?&rdquo;</div>
            <h4>Decisions</h4>
            <ul>
              <li>Launch moves to Dec 4</li>
              <li>Support gets a week to prepare</li>
            </ul>
            <h4>Follow-ups</h4>
            <ul>
              <li>Maya: update the press note</li>
              <li>Sam: brief support by Friday</li>
            </ul>
          </div>
        </div>
      </section>

      <section className="hp-closer">
        <div className="lb-wrap">
          <h2>Talk to it. It gets done.</h2>
          <div className="hp-ctas hp-center">
            <a className="lb-btn lb-btn-s lb-btn-xl" href="#phone">
              Lightbulb Phone
            </a>
            <a className="lb-btn lb-btn-s lb-btn-xl" href="#spark">
              Spark for Mac
            </a>
            <a className="lb-btn lb-btn-s lb-btn-xl" href="#meetings">
              Lightbulb Meetings
            </a>
          </div>
          <p className="hp-founder">
            Not sure which fits? <Link href="/request-setup">Talk to a founder</Link>
          </p>
        </div>
      </section>
    </>
  );
}
