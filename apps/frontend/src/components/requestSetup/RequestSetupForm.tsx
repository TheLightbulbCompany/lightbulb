"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { BACKEND_URL } from "@/lib/api";
import { SUPPORT_EMAIL } from "@/lib/domain";
import { attributionProperties, client as posthog, refreshAttribution } from "@/lib/analytics";

const field = "mt-2 w-full rounded-md border border-[var(--rule)] bg-[var(--cream)] px-3 py-2 text-base text-[var(--lb-ink)]";
const label = "block text-sm font-medium text-[var(--lb-ink)]";
const helper = "mt-1 text-sm text-[var(--lb-ink-soft)]";

/**
 * First-touch attribution the browser already keeps (lib/analytics: UTM,
 * referrer, landing path). Hook for T12: when first-touch moves onto the
 * workspace, read the same store here so a request and a sign-up agree.
 */
function readAttribution(): Record<string, string> {
  refreshAttribution();
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(attributionProperties())) {
    if (typeof v === "string" && v) out[k] = v;
  }
  return out;
}

function anonymousId(): string {
  try {
    return posthog.get_distinct_id?.() ?? "";
  } catch {
    return "";
  }
}

/**
 * "Talk to a founder" for a salon, spa or barbershop owner: four fields, then
 * a founder calls. The payload is `POST /setup-requests` (schemas/setup_request.py);
 * `agents: ["other"]` is the one value that schema still requires.
 */
export function RequestSetupForm({ waitlist }: { waitlist?: "meetings" | "os" } = {}) {
  const [state, setState] = useState<"idle" | "sending" | "error" | "invalid">("idle");
  const [done, setDone] = useState<string | null>(null);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const text = (k: string) => String(form.get(k) ?? "").trim();
    const name = text("name");
    setState("sending");
    try {
      const res = await fetch(`${BACKEND_URL}/setup-requests`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          email: text("email"),
          ...(waitlist ? { waitlist } : { phone: text("phone"), company: text("company") }),
          agents: ["other"],
          attribution: readAttribution(),
          // Read at submit, not with useSearchParams, so the page renders on the server with its headline.
          source_param: (new URLSearchParams(window.location.search).get("source") ?? "").slice(0, 100),
          anonymous_id: anonymousId(),
        }),
      });
      if (res.status === 422 && waitlist) return setState("invalid");
      if (!res.ok) throw new Error(String(res.status));
      setDone(name.split(/\s+/)[0]);
    } catch {
      setState("error");
    }
  }

  if (done && waitlist) {
    return (
      <div role="status">
        <h2 className="font-editorial-serif text-3xl leading-tight text-[var(--lb-ink)]">
          Thanks, {done}. You&apos;re on the list.
        </h2>
        <p className="mt-4 text-lg leading-8 text-[var(--lb-ink-soft)]">
          We&apos;ll email you when Lightbulb {waitlist === "os" ? "OS" : "Meetings"} is ready for you.
        </p>
      </div>
    );
  }

  if (done) {
    return (
      <div role="status">
        <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)]">Thanks, {done}.</h1>
        <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
          A founder reads every request and will call or email you. Rather start now? You can set up your
          assistant yourself in the meantime.
        </p>
        <Link href="/sign-up" className="lb-btn lb-btn-p mt-8 inline-flex">
          Set up your assistant
        </Link>
      </div>
    );
  }

  return (
    <>
      {!waitlist && (
        <>
          <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)] md:text-6xl">Talk to a founder</h1>
          <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
            Rather not set it up alone? Tell us about your salon, spa or barbershop. One of the founders will get in touch
            and set up your assistant with you.
          </p>
        </>
      )}
      <form className={waitlist ? "space-y-8" : "mt-12 space-y-8"} onSubmit={onSubmit}>
        <div>
          <label className={label} htmlFor="rs-name">Your name</label>
          <input id="rs-name" name="name" required autoComplete="name" className={field} />
        </div>
        <div>
          <label className={label} htmlFor="rs-email">Email</label>
          <input id="rs-email" name="email" type="email" required autoComplete="email" className={field} />
        </div>
        {!waitlist && (
          <>
        <div>
          <label className={label} htmlFor="rs-phone">Mobile number</label>
          <input id="rs-phone" name="phone" type="tel" required autoComplete="tel" maxLength={40} className={field} />
          <p className={helper}>A founder will call or text you here.</p>
        </div>
        <div>
          <label className={label} htmlFor="rs-company">Business name</label>
          <input id="rs-company" name="company" required autoComplete="organization" className={field} />
        </div>
          </>
        )}
        <div>
          <button type="submit" className="lb-btn lb-btn-p" disabled={state === "sending"}>
            {state === "sending" ? "Sending…" : waitlist ? "Join the waitlist" : "Send to a founder"}
          </button>
          {!waitlist && <p className={helper}>A founder reads every request.</p>}
          {state === "invalid" && (
            <p role="alert" className="mt-3 text-sm font-medium text-[var(--lb-ink)]">
              Check your email address and try again.
            </p>
          )}
          {state === "error" && (
            <p role="alert" className="mt-3 text-sm font-medium text-[var(--lb-ink)]">
              That did not go through. Try again, or email {SUPPORT_EMAIL}.
            </p>
          )}
        </div>
      </form>
    </>
  );
}
