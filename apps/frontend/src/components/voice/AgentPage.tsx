"use client";

// The Agent page (spec 2026-11-07 §5): one long page, no tabs, every change saved as you make it. Go live once, Pause
// is the brake. One JSON document per workspace (GET/PUT /voice/agent).
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useOrganization } from "@clerk/nextjs";
import { Check as Tick, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { DAYS, nameOf, useAgentConfig, useInviteAddress, type AgentConfig } from "./data";

import { PhoneVoiceSettings } from "./PhoneVoiceSettings";

type Save = "idle" | "saving" | "saved" | "error" | "forbidden" | "payment" | "handoff";

export function AgentPage() {
  const { data: config, error, save, agents = [], frontAgentId, frontAgentIssue } = useAgentConfig();
  const invite = useInviteAddress();
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
      save(next).then(() => setSaveState("saved"),
        async (e: { status?: number; body?: { detail?: { code?: string } } }) => {
          if (e.body?.detail?.code === "voice_payment_required") {
            const pending = { ...next, live: false };
            setDraft(pending);
            try { await save(pending); setSaveState("payment"); }
            catch { setSaveState("error"); }
          } else setSaveState(e?.status === 403 ? "forbidden" : e.body?.detail?.code === "handoff_number_not_us" ? "handoff" : "error");
        });
    }, 600);
  };
  const name = nameOf(c);
  const status = c.paused ? "Paused" : c.live ? "Live" : "Not live yet";

  return <div className="vx-page">
    <header className="vx-agent-head">
      <div>
        <h1 className="vx-title">{name} <span className="vx-muted">· {business}</span></h1>
        <p className="vx-sub"><span className="vx-tag" data-tone={c.live && !c.paused ? "ok" : c.paused ? "warn" : undefined}>{status}</span>
          {c.number ? <> · answering {c.number}</> : <> · no number yet</>}</p>
        <p className="vx-save" role="status" data-err={saveState === "error" || saveState === "forbidden" || saveState === "handoff"} style={{ marginTop: 6 }}>
          {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Saved" : saveState === "error" ? "Couldn't save the last change. Change it again to retry."
            : saveState === "forbidden" ? "Only an admin can change the assistant."
            : saveState === "handoff" ? "The hand-off number must be a US or Canadian number. Change it to save."
            : saveState === "payment" ? "Your setup is saved. Activate your voice plan, then return and choose Go live to get your number."
            : "Changes save as you make them."}</p>
        {saveState === "payment" && <Button asChild variant="outline" size="sm"><Link href="/voice/settings#voice-plan-heading">Activate voice plan</Link></Button>}
      </div>
      <div className="vx-actions" style={{ marginTop: 0 }}>
        {c.number && <Button asChild variant="outline" size="sm"><a href={`tel:${c.number}`}>Test call</a></Button>}
        {!c.live ? <Button size="sm" onClick={() => set({ live: true, paused: false })}>Go live</Button>
          : <Button size="sm" variant="outline" onClick={() => set({ paused: !c.paused })}>{c.paused ? "Resume" : "Pause"}</Button>}
      </div>
    </header>

    <Sec title="Front-door agent" hint="Choose an existing worker to greet people as Lightbulb. Every agent stays on your team and can do work. New conversations use this choice; existing conversations keep their agent.">
      <label className="vx-label">Receptionist
        <select className="vx-field" value={c.frontAgentId ?? ""} onChange={(e) => set({ frontAgentId: e.target.value || null })}>
          <option value="">Workspace default{!c.frontAgentId && frontAgentId ? ` · ${agents.find((a) => a.id === frontAgentId)?.name ?? "existing agent"}` : ""}</option>
          {c.frontAgentId && !agents.some((a) => a.id === c.frontAgentId) && <option value={c.frontAgentId}>Unavailable agent — choose another</option>}
          {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
      </label>
      {frontAgentIssue && <p role="alert">{frontAgentIssue}</p>}
    </Sec>

    <Sec title="Name & greeting">
      <label className="vx-label">Name (callers hear it; in a meeting you say &ldquo;{name}, …&rdquo;)
        <input className="vx-field" value={c.name} maxLength={40} placeholder="Lightbulb" onChange={(e) => set({ name: e.target.value })} /></label>
      <label className="vx-label">Greeting style
        <textarea className="vx-field" rows={3} maxLength={400} value={c.greeting} placeholder={`How ${name} should open: tone, what to mention`} onChange={(e) => set({ greeting: e.target.value })} /></label>
      <p className="vx-hint" style={{ color: "var(--fg-mute)", fontSize: "var(--text-caption)" }}>
        The greeting is yours to write; leave it blank for a short default. Some states require telling callers they are talking to an AI or that calls are recorded: that is your business&apos;s responsibility.</p>
    </Sec>

    <Sec title="Voice & language"><PhoneVoiceSettings c={c} set={set} /></Sec>

    <Sec title={`What ${name} may do for callers`} hint={`Anything unchecked, or anything it doesn't know: ${name} takes a message and puts it in Needs you.`}>
      <div>
        <Check label="Book appointments" on={c.allow.book} set={(book) => set({ allow: { ...c.allow, book } })} />
        <Check label="Move and cancel appointments" on={c.allow.move} set={(move) => set({ allow: { ...c.allow, move } })} />
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
      <Closures c={c} set={set} />
    </Sec>

    <Sec title="Services & staff">
      <ServicesEditor c={c} set={set} />
      <div style={{ display: "flex", gap: 16, alignItems: "end", flexWrap: "wrap" }}>
        <Button size="sm" variant="outline" onClick={() => set({ services: [...c.services, { name: "", minutes: 45, price: "", staff: "" }] })}><Plus /> Add a service</Button>
        <label className="vx-label">Buffer between appointments (min)
          <input className="vx-field" type="number" min={0} step={5} value={c.bufferMin} style={{ width: 96 }} onChange={(e) => set({ bufferMin: Number(e.target.value) })} /></label>
      </div>
    </Sec>

    <Sec title="Calendar & booking system" hint={<>Connect Google Calendar in <Link className="vx-link" href="/chat?panel=settings">Settings › Connections</Link>.</>}>
      <label className="vx-label">Bookings go to
        <select className="vx-field" value={c.bookingsGo === "google" ? "google" : "own"} onChange={(e) => set({ bookingsGo: e.target.value as AgentConfig["bookingsGo"] })}>
          <option value="google">Google Calendar</option>
          <option value="own">{name}&apos;s own calendar{c.bookingsGo === "square" || c.bookingsGo === "other" ? " for now" : ""}</option>
        </select></label>
    </Sec>

    <Sec title="Number & forwarding">
      <p style={{ fontSize: "var(--text-ui)" }}>{name}&apos;s number: {c.number ? <b>{c.number}</b> : <span className="vx-muted">not set up yet</span>}</p>
      <label className="vx-label">Your business line
        <input className="vx-field" type="tel" value={c.forwardFrom} placeholder="(415) 555-0199" onChange={(e) => set({ forwardFrom: e.target.value })} /></label>
      <p className="vx-hint" style={{ fontSize: "var(--text-caption)", color: "var(--fg-mute)" }}>
        To forward it, dial <span className="vx-code">*72</span> then {name}&apos;s number from your business line. Dial <span className="vx-code">*73</span> to stop.</p>
      {c.number && (c.numberChoice ? c.numberChoice.path === "keep" : !!c.forwardFrom) && <ForwardCheck c={c} set={set} />}
    </Sec>

    <Sec title="Hand-off to a person" hint="When a caller asks for a person. Otherwise it takes a message.">
      <label className="vx-label">Transfer to
        <input className="vx-field" type="tel" value={c.handoff.number} placeholder="Your cell" onChange={(e) => set({ handoff: { ...c.handoff, number: e.target.value } })} /></label>
      <Check label="Only during opening hours" on={c.handoff.onlyOpenHours} set={(onlyOpenHours) => set({ handoff: { ...c.handoff, onlyOpenHours } })} />
    </Sec>

    <Sec title="Meetings" hint={<>Invite {invite ? <span className="vx-code">{invite}</span> : "the assistant's team address (Mail)"} to any Google Meet. {name} joins and listens. Say &ldquo;{name}&rdquo; to bring it into the conversation; while active, it responds naturally. Guests are told it&apos;s there; anyone can say &ldquo;{name}, don&apos;t transcribe me&rdquo;.</>}>
      <div>
        <Check label="Turn action items into follow-ups" on={c.meetings.followUps} set={(followUps) => set({ meetings: { ...c.meetings, followUps } })} />
      </div>
    </Sec>

    <Sec title="Reminders & follow-ups">
      <div>
        <Check label="Text clients the day before their appointment" on={false} disabled note="Not available yet" />
        <Check label="Text back missed callers who didn't leave a message" on={false} disabled note="Not available yet" />
        <Check label="Text the waitlist when a slot frees up" on={false} disabled note="Not available yet" />
      </div>
    </Sec>
  </div>;
}

export function Sec({ title, hint, children }: { title: string; hint?: React.ReactNode; children: React.ReactNode }) {
  return <section className="vx-sec"><h2>{title}</h2>{hint && <p className="vx-hint">{hint}</p>}<div className="vx-grid">{children}</div></section>;
}

/** Closed dates, stored as `holidays: [{from, to}]` (local ISO dates, inclusive); the old free-text value shows as none. */
export function Closures({ c, set }: Edit) {
  const list = Array.isArray(c.holidays) ? c.holidays : [];
  const put = (n: number, patch: Partial<(typeof list)[number]>) => set({ holidays: list.map((r, i) => (i === n ? { ...r, ...patch } : r)) });
  return <div>
    <b className="vx-label">Holidays and closures</b>
    {list.map((r, n) => <div key={n} style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
      <input className="vx-field" type="date" aria-label="Closed from" value={r.from} onChange={(e) => put(n, { from: e.target.value, to: r.to < e.target.value ? e.target.value : r.to })} />
      <span className="vx-muted">to</span>
      <input className="vx-field" type="date" aria-label="Closed until" value={r.to} min={r.from} onChange={(e) => put(n, { to: e.target.value })} />
      <Button size="icon-sm" variant="ghost" aria-label="Remove closure" onClick={() => set({ holidays: list.filter((_, i) => i !== n) })}><X /></Button>
    </div>)}
    <div style={{ marginTop: 8 }}><Button size="sm" variant="outline" onClick={() => { const d = new Intl.DateTimeFormat("en-CA", { timeZone: c.timezone || undefined }).format(new Date()); set({ holidays: [...list, { from: d, to: d }] }); }}><Plus /> Add a closed date</Button></div>
  </div>;
}

export function Check({ label, on, set, disabled, note }: { label: string; on: boolean; set?: (v: boolean) => void; disabled?: boolean; note?: string }) {
  return <label className="vx-check" data-off={disabled}>
    <input type="checkbox" checked={on} disabled={disabled} onChange={(e) => set?.(e.target.checked)} />{label}{note && <small className="vx-muted">{note}</small>}</label>;
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

/** What the server's refusals say; any other refusal reads as a missed call. */
const REFUSED: Record<string, string> = {
  outbound_not_configured: "Forwarding check isn't switched on yet",
  check_in_progress: "A check is already running",
  us_only: "Forwarding checks work for US and Canadian numbers for now",
  daily_limit: "That's five checks today. Try again tomorrow",
};
const POLL_MS = 3000, POLL_FOR_MS = 70_000;  // as long as the server counts a call as the probe (voice_outbound.WINDOW_S)

/** Rings the business line once from the assistant's number; forwarding sends it back, and the server marks it. */
export function ForwardCheck({ c, set }: { c: AgentConfig; set: (patch: Partial<AgentConfig>) => void }) {
  const api = useApi();
  const [state, setState] = useState<string>(c.forwardingOk ? "ok" : "");
  const check = async () => {
    setState("checking");
    try {
      const r = (await api.post("/voice/forward-check", { forwardFrom: c.forwardFrom })) as { ok: boolean; reason?: string };
      if (!r.ok) return setState(r.reason && r.reason in REFUSED ? r.reason : "missed");
      for (let t = POLL_MS; t <= POLL_FOR_MS; t += POLL_MS) {  // every 3 s, for up to 70 s
        await new Promise((done) => setTimeout(done, POLL_MS));
        const { config } = (await api.get("/voice/agent")) as { config: AgentConfig | null };
        if (config?.forwardingOk) { set({ forwardingOk: true }); return setState("ok"); }  // kept by the next save
      }
      setState("missed");
    } catch { setState("missed"); }
  };
  return <div style={{ display: "grid", gap: 8, justifyItems: "start" }}>
    <Button variant="outline" disabled={!c.forwardFrom.trim() || state === "checking"} onClick={check}>
      {state === "checking" ? "Checking forwarding…" : "Check forwarding"}</Button>
    <p className="vx-sub" role="status">
      {state === "ok" && <><Tick size={16} aria-hidden style={{ verticalAlign: "-3px" }} /> Forwarding works. Calls to your line reach your assistant.</>}
      {state === "missed" && "We didn't see the call arrive. Check the code for your carrier and try again."}
      {REFUSED[state]}
    </p>
  </div>;
}
