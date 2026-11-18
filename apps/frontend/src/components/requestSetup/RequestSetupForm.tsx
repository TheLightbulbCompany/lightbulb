"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { BACKEND_URL } from "@/lib/api";
import { SUPPORT_EMAIL } from "@/lib/domain";
import { attributionProperties, client as posthog, refreshAttribution } from "@/lib/analytics";

import { BOOKING_TOOLS, SOURCES, TEAM_SIZES } from "./options";

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
 * "Talk to a founder" for a salon, spa or barbershop owner. The payload keeps
 * the shape of `POST /setup-requests` (schemas/setup_request.py): the booking
 * tool rides in `agents_other` with `agents: ["other"]`, "what happens to your
 * calls" in `first_handoff`, and the website in `thread_link`.
 */
export function RequestSetupForm() {
  const params = useSearchParams();
  const [state, setState] = useState<"idle" | "sending" | "error">("idle");
  const [done, setDone] = useState<string | null>(null);
  const [source, setSource] = useState("");

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
          company: text("company"),
          team_size: text("team_size"),
          agents: ["other"],
          agents_other: `Bookings: ${text("bookings")}`,
          first_handoff: text("first_handoff"),
          source,
          source_other: text("source_other"),
          thread_link: text("website"),
          attribution: readAttribution(),
          source_param: (params?.get("source") ?? "").slice(0, 100),
          anonymous_id: anonymousId(),
        }),
      });
      if (!res.ok) throw new Error(String(res.status));
      setDone(name.split(/\s+/)[0]);
    } catch {
      setState("error");
    }
  }

  if (done) {
    return (
      <div role="status">
        <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)]">Thanks, {done}.</h1>
        <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
          A founder reads every request and will email you to find a time. Rather start now? You can set up your
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
      <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)] md:text-6xl">Talk to a founder</h1>
      <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
        Rather not set it up alone? Tell us about your salon, spa or barbershop. One of the founders will get in touch
        and set up your assistant with you.
      </p>
      <form className="mt-12 space-y-8" onSubmit={onSubmit}>
        <div>
          <label className={label} htmlFor="rs-name">Your name</label>
          <input id="rs-name" name="name" required autoComplete="name" className={field} />
        </div>
        <div>
          <label className={label} htmlFor="rs-email">Email</label>
          <input id="rs-email" name="email" type="email" required autoComplete="email" className={field} />
          <p className={helper}>We reply here.</p>
        </div>
        <div>
          <label className={label} htmlFor="rs-company">Business name</label>
          <input id="rs-company" name="company" required autoComplete="organization" className={field} />
        </div>
        <fieldset>
          <legend className={label}>How many people work there, including you?</legend>
          <div className="mt-3 flex flex-wrap gap-4">
            {TEAM_SIZES.map((o) => (
              <label key={o.value} className="flex items-center gap-2 text-[var(--lb-ink)]">
                <input type="radio" name="team_size" value={o.value} required /> {o.label}
              </label>
            ))}
          </div>
        </fieldset>
        <div>
          <label className={label} htmlFor="rs-bookings">Where do your bookings live today?</label>
          <select id="rs-bookings" name="bookings" required defaultValue="" className={field}>
            <option value="" disabled>
              Choose one
            </option>
            {BOOKING_TOOLS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className={label} htmlFor="rs-calls">What happens to your calls today?</label>
          <textarea id="rs-calls" name="first_handoff" required rows={3} maxLength={2000} className={field} />
          <p className={helper}>
            For example: &ldquo;I miss calls while I&rsquo;m with clients, and nobody answers after 7 pm.&rdquo;
          </p>
        </div>
        <div>
          <label className={label} htmlFor="rs-website">Your website (optional)</label>
          <input id="rs-website" name="website" inputMode="url" maxLength={500} autoComplete="url" className={field} />
        </div>
        <div>
          <label className={label} htmlFor="rs-source">How did you first hear about us?</label>
          <select
            id="rs-source"
            name="source"
            required
            value={source}
            onChange={(e) => setSource(e.target.value)}
            className={field}
          >
            <option value="" disabled>
              Choose one
            </option>
            {SOURCES.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          {source === "other" && <input name="source_other" required aria-label="Where did you hear about us?" className={field} />}
        </div>
        <div>
          <button type="submit" className="lb-btn lb-btn-p" disabled={state === "sending"}>
            {state === "sending" ? "Sending…" : "Send to a founder"}
          </button>
          <p className={helper}>A founder reads every request.</p>
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
