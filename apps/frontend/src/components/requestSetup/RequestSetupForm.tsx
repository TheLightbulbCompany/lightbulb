"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { BACKEND_URL } from "@/lib/api";
import { SUPPORT_EMAIL } from "@/lib/domain";
import { readHeadlineVariantCookie } from "@/lib/headlineVariant";
import { attributionProperties, client as posthog, refreshAttribution } from "@/lib/analytics";

import { AGENTS, SMALL_TEAMS, SOURCES, TEAM_SIZES } from "./options";

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

export function RequestSetupForm() {
  const params = useSearchParams();
  const [state, setState] = useState<"idle" | "sending" | "error">("idle");
  const [done, setDone] = useState<{ firstName: string; small: boolean } | null>(null);
  const [agents, setAgents] = useState<string[]>([]);
  const [source, setSource] = useState("");

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const text = (k: string) => String(form.get(k) ?? "").trim();
    const name = text("name");
    const teamSize = text("team_size");
    setState("sending");
    try {
      const res = await fetch(`${BACKEND_URL}/setup-requests`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          email: text("email"),
          company: text("company"),
          team_size: teamSize,
          agents,
          agents_other: text("agents_other"),
          first_handoff: text("first_handoff"),
          source,
          source_other: text("source_other"),
          thread_link: text("thread_link"),
          attribution: readAttribution(),
          source_param: (params?.get("source") ?? "").slice(0, 100),
          anonymous_id: anonymousId(),
          headline_variant: readHeadlineVariantCookie(),
        }),
      });
      if (!res.ok) throw new Error(String(res.status));
      setDone({ firstName: name.split(/\s+/)[0], small: SMALL_TEAMS.has(teamSize) });
    } catch {
      setState("error");
    }
  }

  if (done) {
    return (
      <div role="status">
        <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)]">Thanks, {done.firstName}.</h1>
        {done.small ? (
          <>
            <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
              The guided setups are for teams of three or more, but you can start your 30 days today and invite
              people as you go.
            </p>
            <Link href="/sign-up" className="lb-btn lb-btn-p mt-8 inline-flex">
              Start free
            </Link>
          </>
        ) : (
          <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
            One of us will email you within a day to book 45 minutes. Bring the person you&rsquo;d hand work to
            first.
          </p>
        )}
      </div>
    );
  }

  return (
    <>
      <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)] md:text-6xl">Request a guided setup</h1>
      <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
        One of the founders sets your team up with you in 45 minutes: one real tool, one real workflow, your second
        person in the room. Ten spots.
      </p>
      <form className="mt-12 space-y-8" onSubmit={onSubmit}>
        <div>
          <label className={label} htmlFor="rs-name">Your name</label>
          <input id="rs-name" name="name" required autoComplete="name" className={field} />
        </div>
        <div>
          <label className={label} htmlFor="rs-email">Work email</label>
          <input id="rs-email" name="email" type="email" required autoComplete="email" className={field} />
          <p className={helper}>We reply here within a day.</p>
        </div>
        <div>
          <label className={label} htmlFor="rs-company">Company</label>
          <input id="rs-company" name="company" required autoComplete="organization" className={field} />
        </div>
        <fieldset>
          <legend className={label}>Team size</legend>
          <p className={helper}>How many people would use it?</p>
          <div className="mt-3 flex flex-wrap gap-4">
            {TEAM_SIZES.map((o) => (
              <label key={o.value} className="flex items-center gap-2 text-[var(--lb-ink)]">
                <input type="radio" name="team_size" value={o.value} required /> {o.label}
              </label>
            ))}
          </div>
        </fieldset>
        <fieldset>
          <legend className={label}>Agents you use today</legend>
          <p className={helper}>Pick all you use for real work.</p>
          <div className="mt-3 flex flex-wrap gap-4">
            {AGENTS.map((o) => (
              <label key={o.value} className="flex items-center gap-2 text-[var(--lb-ink)]">
                <input
                  type="checkbox"
                  value={o.value}
                  checked={agents.includes(o.value)}
                  onChange={(e) =>
                    setAgents((a) => (e.target.checked ? [...a, o.value] : a.filter((x) => x !== o.value)))
                  }
                />{" "}
                {o.label}
              </label>
            ))}
          </div>
          {agents.includes("other") && (
            <input name="agents_other" aria-label="Other agents" className={field} />
          )}
        </fieldset>
        <div>
          <label className={label} htmlFor="rs-handoff">What would you hand off first?</label>
          <textarea id="rs-handoff" name="first_handoff" required rows={3} className={field} />
          <p className={helper}>
            One job. For example: &ldquo;weekly investor update&rdquo; or &ldquo;triage new GitHub issues&rdquo;.
          </p>
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
          <label className={label} htmlFor="rs-thread">Link to the post or thread (optional)</label>
          <input id="rs-thread" name="thread_link" type="url" className={field} />
        </div>
        <div>
          <button
            type="submit"
            className="lb-btn lb-btn-p"
            disabled={state === "sending" || agents.length === 0}
          >
            {state === "sending" ? "Sending…" : "Request my setup"}
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
