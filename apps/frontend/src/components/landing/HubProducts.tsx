"use client";

import Link from "next/link";

import { captureSignupCta } from "@/lib/signupFlow";

import { Arrow } from "./Arrow";

/** The company home's three doors: sign up for Phone, or join a waitlist. */
export function HubProducts() {
  return (
    <>
      <ul className="lb-hub-cards">
        <li className="lb-plan lb-hub-card">
          <h2>Lightbulb Phone</h2>
          <p>Answers your calls day and night, books appointments, takes messages and hands callers to you.</p>
          <div>
            <Link
              href="/sign-up"
              className="lb-btn lb-btn-p lb-btn-xl"
              onClick={() =>
                captureSignupCta({
                  ctaId: "hub_sign_up",
                  label: "Set up Lightbulb Phone",
                  surface: "hero",
                  destination: "/sign-up",
                })
              }
            >
              Set up Lightbulb Phone
              <Arrow />
            </Link>
            <Link href="/phone" className="lb-hub-more">
              See how it works
            </Link>
          </div>
        </li>
        <li className="lb-plan lb-hub-card">
          <h2>Lightbulb Meetings</h2>
          <p>Joins your Google Meet when invited, speaks only when named, and leaves notes, decisions and follow-ups.</p>
          <div>
            <Link href="/meetings" className="lb-btn lb-btn-s lb-btn-xl">
              Join the waitlist
            </Link>
          </div>
        </li>
        <li className="lb-plan lb-hub-card">
          <h2>Lightbulb OS</h2>
          <p>A voice app for the Mac. Not out yet.</p>
          <div>
            <Link href="/os" className="lb-btn lb-btn-s lb-btn-xl">
              Join the waitlist
            </Link>
          </div>
        </li>
      </ul>
      <p className="lb-hub-founder">
        Not sure which fits? <Link href="/request-setup">Talk to a founder</Link>
      </p>
    </>
  );
}
