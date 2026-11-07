"use client";

// The Agent page (spec 2026-11-07 §5): one long page, no tabs, every change saved as you make it. Go live once, Pause
// is the brake. One JSON document per workspace (GET/PUT /voice/agent).
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useOrganization } from "@clerk/nextjs";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DAYS, nameOf, useAgentConfig, type AgentConfig } from "./data";

type Save = "idle" | "saving" | "saved" | "error" | "forbidden";

export function AgentPage() {
  const { data: config, error, save } = useAgentConfig();
  const { organization } = useOrganization();
  const business = organization?.name || "your business";
  const [draft, setDraft] = useState<AgentConfig | null>(null);
  const [saveState, setSaveState] = useState<Save>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  if (error) return <div className="vx-page"><p className="vx-empty" role="alert">Couldn&apos;t load the assistant&apos;s settings. Reload to try again.</p></div>;
  // The page edits a local copy from the first change on, so a revalidation never overwrites what is being typed.
  const c = draft ?? config;
  if (!c) return <div className="vx-page"><p className="vx-empty">Loading…</p></div>;

  const set = (patch: Partial<AgentConfig>) => {
    const next = { ...c, ...patch };
    setDraft(next); setSaveState("saving");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      save(next).then(() => setSaveState("saved"), (e: { status?: number }) => setSaveState(e?.status === 403 ? "forbidden" : "error"));
    }, 600);
  };
  const name = nameOf(c);
  const greeting = c.greeting || defaultGreeting(business, name);
  const lawful = isLawful(greeting);
  const status = c.paused ? "Paused" : c.live ? "Live" : "Not live yet";

  return <div className="vx-page">
    <header className="vx-agent-head">
      <div>
        <h1 className="vx-title">{name} <span className="vx-muted">· {business}</span></h1>
        <p className="vx-sub"><span className="vx-tag" data-tone={c.live && !c.paused ? "ok" : c.paused ? "warn" : undefined}>{status}</span>
          {c.number ? <> · answering {c.number}</> : <> · no number yet</>}</p>
        <p className="vx-save" role="status" data-err={saveState === "error" || saveState === "forbidden"} style={{ marginTop: 6 }}>
          {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Saved" : saveState === "error" ? "Couldn't save the last change. Change it again to retry."
            : saveState === "forbidden" ? "Only an admin can change the assistant." : "Changes save as you make them."}</p>
      </div>
      <div className="vx-actions" style={{ marginTop: 0 }}>
        {c.number && <Button asChild variant="outline" size="sm"><a href={`tel:${c.number}`}>Test call</a></Button>}
        {!c.live ? <Button size="sm" onClick={() => set({ live: true, paused: false })}>Go live</Button>
          : <Button size="sm" variant="outline" onClick={() => set({ paused: !c.paused })}>{c.paused ? "Resume" : "Pause"}</Button>}
      </div>
    </header>

    <Sec title="Name, greeting & voice">
      <label className="vx-label">Name (callers hear it; in a meeting you say &ldquo;{name}, …&rdquo;)
        <input className="vx-field" value={c.name} maxLength={40} placeholder="Nova" onChange={(e) => set({ name: e.target.value })} /></label>
      <label className="vx-label">Greeting
        <textarea className="vx-field" rows={3} maxLength={400} value={c.greeting} placeholder={greeting} onChange={(e) => set({ greeting: e.target.value })} /></label>
      <p className="vx-hint" style={{ color: lawful ? "var(--fg-mute)" : "var(--err)", fontSize: "var(--text-caption)" }}>
        {lawful ? "“AI assistant” and “this call is recorded” stay in the first sentence: that is what the recording and AI-disclosure laws want. No real person's name, no cloned voice."
          : "Keep “AI assistant” and “this call is recorded” in the greeting: the recording and AI-disclosure laws want both."}</p>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "end" }}>
        <label className="vx-label">Voice
          <select className="vx-field" value={c.voice} onChange={(e) => set({ voice: e.target.value })}>
            {["Warm", "Bright", "Calm"].map((v) => <option key={v}>{v}</option>)}</select></label>
        <Check label="Also speaks Spanish" on={c.spanish} set={(spanish) => set({ spanish })} />
      </div>
    </Sec>

    <Sec title={`What ${name} may do for callers`} hint={`Anything unchecked, or anything it doesn't know: ${name} takes a message and puts it in Needs you.`}>
      <div>
        <Check label="Book appointments" on={c.allow.book} set={(book) => set({ allow: { ...c.allow, book } })} />
        <Check label="Move and cancel appointments" on={c.allow.move} set={(move) => set({ allow: { ...c.allow, move } })} />
        <Check label="Add callers to the waitlist" on={c.allow.waitlist} set={(waitlist) => set({ allow: { ...c.allow, waitlist } })} />
        <Check label="Greet returning clients by name" on={c.allow.greetByName} set={(greetByName) => set({ allow: { ...c.allow, greetByName } })} />
        <Check label="Answer prices, hours, services and directions" on={c.allow.answer} set={(answer) => set({ allow: { ...c.allow, answer } })} />
        <Check label="Take deposits or payments (not available yet)" on={false} disabled />
      </div>
    </Sec>

    <Sec title={`Knowledge: what ${name} knows about ${business}`}>
      <KnowledgeList c={c} set={set} />
      <ToAdd c={c} set={set} name={name} />
      <label className="vx-label">Instructions (anything else {name} should know or how it should behave)
        <textarea className="vx-field" rows={3} maxLength={4000} value={c.instructions} onChange={(e) => set({ instructions: e.target.value })} /></label>
    </Sec>

    <Sec title="Hours" hint={`${name} answers 24/7; it only books inside these hours.`}>
      <HoursEditor c={c} set={set} />
      <label className="vx-label">Holidays and closures
        <input className="vx-field" value={c.holidays} placeholder="Closed 26–27 Nov" onChange={(e) => set({ holidays: e.target.value })} /></label>
    </Sec>

    <Sec title="Services & staff">
      <ServicesEditor c={c} set={set} />
      <div style={{ display: "flex", gap: 16, alignItems: "end", flexWrap: "wrap" }}>
        <Button size="sm" variant="outline" onClick={() => set({ services: [...c.services, { name: "", minutes: 45, price: "", staff: "" }] })}><Plus /> Add a service</Button>
        <label className="vx-label">Buffer between appointments (min)
          <input className="vx-field" type="number" min={0} step={5} value={c.bufferMin} style={{ width: 96 }} onChange={(e) => set({ bufferMin: Number(e.target.value) })} /></label>
      </div>
    </Sec>

    <Sec title="Calendar & booking system" hint={<>Connect Google Calendar or Square in <Link className="vx-link" href="/chat?panel=settings">Settings › Connections</Link>.</>}>
      <label className="vx-label">Bookings go to
        <select className="vx-field" value={c.bookingsGo} onChange={(e) => set({ bookingsGo: e.target.value as AgentConfig["bookingsGo"] })}>
          <option value="google">Google Calendar</option><option value="square">Square</option>
          <option value="other">Another booking system (Fresha, GlossGenius, Booksy, Vagaro…)</option>
          <option value="own">{name}&apos;s own calendar for now</option>
        </select></label>
      {c.bookingsGo === "other" && <label className="vx-label">Which one
        <input className="vx-field" value={c.otherSystem} placeholder="Fresha" onChange={(e) => set({ otherSystem: e.target.value })} />
        <span className="vx-muted">{name} writes each booking there after the call, using your sign-in.</span></label>}
    </Sec>

    <Sec title="Number & forwarding">
      <p style={{ fontSize: "var(--text-ui)" }}>{name}&apos;s number: {c.number ? <b>{c.number}</b> : <span className="vx-muted">not set up yet</span>}</p>
      <label className="vx-label">Your business line
        <input className="vx-field" type="tel" value={c.forwardFrom} placeholder="(415) 555-0199" onChange={(e) => set({ forwardFrom: e.target.value })} /></label>
      <p className="vx-hint" style={{ fontSize: "var(--text-caption)", color: "var(--fg-mute)" }}>
        To forward it, dial <span className="vx-code">*72</span> then {name}&apos;s number from your business line. Dial <span className="vx-code">*73</span> to stop.</p>
    </Sec>

    <Sec title="Hand-off to a person" hint="When a caller asks for a person. Otherwise it takes a message.">
      <label className="vx-label">Transfer to
        <input className="vx-field" type="tel" value={c.handoff.number} placeholder="Your cell" onChange={(e) => set({ handoff: { ...c.handoff, number: e.target.value } })} /></label>
      <Check label="Only during opening hours" on={c.handoff.onlyOpenHours} set={(onlyOpenHours) => set({ handoff: { ...c.handoff, onlyOpenHours } })} />
    </Sec>

    <Sec title="Meetings" hint={<>Invite <span className="vx-code">agents@thelightbulb.company</span> to any Google Meet. {name} joins, listens, and speaks only when someone says &ldquo;{name}&rdquo;. Guests are told it&apos;s there; anyone can say &ldquo;{name}, don&apos;t transcribe me&rdquo;.</>}>
      <div>
        <Check label="Turn action items into follow-ups" on={c.meetings.followUps} set={(followUps) => set({ meetings: { ...c.meetings, followUps } })} />
        <Check label="Email me the notes after" on={c.meetings.emailNotes} set={(emailNotes) => set({ meetings: { ...c.meetings, emailNotes } })} />
      </div>
    </Sec>

    <Sec title="Reminders & follow-ups">
      <div>
        <Check label="Text clients the day before their appointment" on={c.reminders.dayBefore} set={(dayBefore) => set({ reminders: { ...c.reminders, dayBefore } })} />
        <Check label="Text back missed callers who didn't leave a message" on={c.reminders.missedCallers} set={(missedCallers) => set({ reminders: { ...c.reminders, missedCallers } })} />
        <Check label="Text the waitlist when a slot frees up" on={c.reminders.waitlist} set={(waitlist) => set({ reminders: { ...c.reminders, waitlist } })} />
      </div>
    </Sec>
  </div>;
}

export function Sec({ title, hint, children }: { title: string; hint?: React.ReactNode; children: React.ReactNode }) {
  return <section className="vx-sec"><h2>{title}</h2>{hint && <p className="vx-hint">{hint}</p>}<div className="vx-grid">{children}</div></section>;
}

export function Check({ label, on, set, disabled }: { label: string; on: boolean; set?: (v: boolean) => void; disabled?: boolean }) {
  return <label className="vx-check" data-off={disabled}>
    <input type="checkbox" checked={on} disabled={disabled} onChange={(e) => set?.(e.target.checked)} />{label}</label>;
}

export function KnowledgeList({ c, set }: { c: AgentConfig; set: (p: Partial<AgentConfig>) => void }) {
  const put = (n: number, patch: Partial<AgentConfig["knowledge"][number]>) =>
    set({ knowledge: c.knowledge.map((k, i) => (i === n ? { ...k, ...patch } : k)) });
  return <div>
    {c.knowledge.length === 0 && <p className="vx-muted" style={{ fontSize: "var(--text-caption)" }}>Nothing yet. Add the questions callers ask most.</p>}
    {c.knowledge.map((k, n) => <div key={n} className="vx-qa">
      <div style={{ display: "flex", gap: 8 }}>
        <input className="vx-field" aria-label="Question" value={k.q} placeholder="A question callers ask (or leave blank for a note)" style={{ fontWeight: 600 }} onChange={(e) => put(n, { q: e.target.value })} />
        <Button size="icon-sm" variant="ghost" aria-label="Remove" onClick={() => set({ knowledge: c.knowledge.filter((_, i) => i !== n) })}><X /></Button>
      </div>
      <textarea className="vx-field" aria-label="Answer" rows={2} value={k.a} placeholder="The answer" onChange={(e) => put(n, { a: e.target.value })} />
      {k.source && <small>{k.source}</small>}
    </div>)}
    <Button size="sm" variant="outline" style={{ marginTop: 8 }} onClick={() => set({ knowledge: [...c.knowledge, { q: "", a: "" }] })}><Plus /> Add an answer</Button>
  </div>;
}

type Edit = { c: AgentConfig; set: (p: Partial<AgentConfig>) => void };

export const defaultGreeting = (business: string, name: string) =>
  `Thanks for calling ${business}. This is ${name}, ${business}'s AI assistant. This call is recorded. How can I help?`;
export const isLawful = (greeting: string) => /AI assistant/i.test(greeting) && /recorded/i.test(greeting);

export function HoursEditor({ c, set }: Edit) {
  return <>
  <div className="vx-hours">{DAYS.map((d) => {
    const h = c.hours[d] ?? { open: "09:00", close: "18:00" };
    const put = (patch: Partial<typeof h>) => set({ hours: { ...c.hours, [d]: { ...h, ...patch } } });
    return <div key={d} className="contents">
      <span>{d}</span>
      <input type="time" className="vx-field" aria-label={`${d} opens`} value={h.open} disabled={h.closed} onChange={(e) => put({ open: e.target.value })} />
      <input type="time" className="vx-field" aria-label={`${d} closes`} value={h.close} disabled={h.closed} onChange={(e) => put({ close: e.target.value })} />
      <Check label="Closed" on={!!h.closed} set={(closed) => put({ closed })} />
    </div>;
  })}</div>
  </>;
}

export function ServicesEditor({ c, set }: Edit) {
  return <>
  {c.services.map((s, n) => {
    const put = (patch: Partial<typeof s>) => set({ services: c.services.map((x, i) => (i === n ? { ...x, ...patch } : x)) });
    return <div key={n} className="vx-svc">
      <input className="vx-field" aria-label="Service" value={s.name} placeholder="Gel manicure" onChange={(e) => put({ name: e.target.value })} />
      <input className="vx-field" aria-label="Minutes" type="number" min={5} step={5} value={s.minutes} data-guessed={s.minutesGuessed || undefined}
        title={s.minutesGuessed ? "A guess: the site doesn't say how long this takes" : undefined}
        onChange={(e) => put({ minutes: Number(e.target.value), minutesGuessed: false })} />
      <input className="vx-field" aria-label="Price" value={s.price} placeholder="$55" onChange={(e) => put({ price: e.target.value })} />
      <input className="vx-field" aria-label="Who does it" value={s.staff} placeholder="Ana, Mei" onChange={(e) => put({ staff: e.target.value })} />
      <Button size="icon-sm" variant="ghost" aria-label={`Remove ${s.name || "service"}`} onClick={() => set({ services: c.services.filter((_, i) => i !== n) })}><X /></Button>
    </div>;
  })}
  </>;
}

export function ToAdd({ c, set, name, why }: Edit & { name: string; why?: string }) {
  return <>
  {c.toAdd.length > 0 && <div className="vx-card" style={{ marginTop: 12 }}>
    <p className="vx-h">{c.toAdd.length} question{c.toAdd.length === 1 ? "" : "s"} to add</p>
    <p className="vx-sub">{why ?? `Callers asked these and ${name} didn't know.`}</p>
    <ul className="vx-grid" style={{ gap: 6 }}>{c.toAdd.map((q) => <li key={q} style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
      <span style={{ fontSize: "var(--text-ui)" }}>{q}</span>
      <Button size="sm" variant="outline" onClick={() => set({ knowledge: [...c.knowledge, { q, a: "", source: "a caller asked" }], toAdd: c.toAdd.filter((t) => t !== q) })}>Answer</Button>
    </li>)}</ul>
  </div>}
  </>;
}
