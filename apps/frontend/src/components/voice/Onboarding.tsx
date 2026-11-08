"use client";

// First run (spec 2026-11-07 §5, approved mockup "onboarding"): five steps, live within 15 minutes, from a phone.
// (1) the business → the assistant reads its website; (2) check what it learned, on the Agent page's own cards;
// (3) where bookings go; (4) its number and forwarding; (5) name, greeting, voice, test call, Go live.
// Every step after the first saves the Agent page document with `onboardingStep`, so leaving (e.g. for the Google
// consent screen) resumes where the owner was. Shapes: routers/voice.py, core/services/voice_onboarding.py.
import { useEffect, useState } from "react";
import { useOrganization } from "@clerk/nextjs";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { useConnections } from "@/hooks/useConnections";
import { ForwardCheck, HoursEditor, KnowledgeList, Sec, ServicesEditor, ToAdd } from "./AgentPage";
import { FORWARDING_CODES, IPHONE_NOTE, CHARGES_NOTE, forwardingCodes, type Carrier, type ForwardMode } from "./forwarding";
import { DAYS, DEFAULT_CONFIG, nameOf, usPhone, type AgentConfig } from "./data";

const NAMES = ["Lightbulb", "Nova", "Sol", "Remy"];

interface Draft {
  read: boolean; hours: AgentConfig["hours"]; services: AgentConfig["services"]; staff: string[];
  knowledge: AgentConfig["knowledge"]; toAdd: string[]; phone: string;
}

/** Fold the website draft into the document, filling only what is still empty: a second read of the site (Back to
 * step 1, read again) never overwrites what the owner already set. Hours count as empty while they are the defaults. */
export function applyDraft(c: AgentConfig, d: Draft): AgentConfig {
  const staff = d.staff.length ? [{ q: "Who works here?", a: d.staff.join(", "), source: "from site" }] : [];
  const asked = new Set(c.knowledge.map((k) => k.q.trim().toLowerCase()));
  const defaultHours = JSON.stringify(c.hours) === JSON.stringify(DEFAULT_CONFIG.hours);
  return {
    ...c,
    hours: defaultHours && Object.keys(d.hours).length === DAYS.length ? d.hours : c.hours,
    services: c.services.length ? c.services : d.services,
    knowledge: [...c.knowledge, ...[...d.knowledge, ...staff].filter((k) => !asked.has(k.q.trim().toLowerCase()))],
    toAdd: c.toAdd.length ? c.toAdd : d.toAdd.filter((q) => !asked.has(q.trim().toLowerCase())),
    forwardFrom: c.forwardFrom || d.phone,
  };
}

/** The hours are in the owner's own zone; keep one already chosen. */
export const withTimezone = (c: AgentConfig): AgentConfig => ({ ...c, timezone: c.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone });

export function Onboarding({ config, save }: { config: AgentConfig; save: (c: AgentConfig) => Promise<void> }) {
  const { organization } = useOrganization();
  const [c, setC] = useState<AgentConfig>(() => ({ ...config, business: config.business || organization?.name || "" }));
  const [step, setStep] = useState(config.onboardingStep || 1);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (patch: Partial<AgentConfig>) => setC((x) => ({ ...x, ...patch }));
  useEffect(() => { if (!c.business && organization?.name) set({ business: organization.name }); }, [organization?.name]); // eslint-disable-line react-hooks/exhaustive-deps

  const go = async (next: AgentConfig, to: number) => {
    setBusy(true); setErr("");
    try {
      const doc = to > 5 ? { ...withTimezone(next), live: true, paused: false, onboardingStep: undefined } : { ...withTimezone(next), onboardingStep: to };
      await save(doc);
      setC(doc); setStep(Math.min(to, 5));
      window.scrollTo?.({ top: 0 });
    } catch (e) {
      const status = (e as { status?: number }).status;
      const code = (e as { body?: { detail?: { code?: string } } }).body?.detail?.code;
      setErr(status === 403 ? "Only an admin of this workspace can set up the assistant."
        : status === 409 && code === "number_unavailable" ? "We couldn't get a number just now. Your setup is saved; try Go live again from the Agent page."
        : "Couldn't save. Try again.");
    } finally { setBusy(false); }
  };

  const back = step > 1 && <Button type="button" variant="ghost" size="sm" onClick={() => setStep(step - 1)}><ArrowLeft /> Back</Button>;
  const props = { c, set, go, busy, back };
  return <div className="vx-onb">
    <div className="vx-steps-bar" aria-hidden="true">{[1, 2, 3, 4, 5].map((n) => <span key={n} data-on={n <= step} />)}</div>
    <p className="vx-step">Step {step} of 5</p>
    {step === 1 && <Business {...props} />}
    {step === 2 && <Learned {...props} />}
    {step === 3 && <Bookings {...props} />}
    {step === 4 && <NumberStep {...props} />}
    {step === 5 && <NameStep {...props} />}
    {err && <p className="vx-save" data-err="true" role="alert" style={{ marginTop: 12 }}>{err}</p>}
  </div>;
}

type StepProps = {
  c: AgentConfig; set: (p: Partial<AgentConfig>) => void; go: (c: AgentConfig, to: number) => Promise<void>;
  busy: boolean; back: React.ReactNode;
};

function Foot({ back, children }: { back: React.ReactNode; children: React.ReactNode }) {
  return <div className="vx-foot-actions">{back}<span className="vx-spacer" />{children}</div>;
}

function Business({ c, set, go, busy }: StepProps) {
  const api = useApi();
  const [reading, setReading] = useState(false);
  const [note, setNote] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const base = { ...c, handoff: { ...c.handoff, number: c.handoff.number.trim() } };
    if (!c.website?.trim()) return go(base, 2);
    setReading(true); setNote("");
    try {
      const { draft } = (await api.post("/voice/onboarding/draft", { website: c.website.trim(), business: c.business ?? "", city: c.city ?? "" })) as { draft: Draft };
      if (!draft.read) setNote("Couldn't open that site. Fill in the cards below; you can change them any time.");
      await go(applyDraft(base, draft), 2);
    } catch {
      await go(base, 2);
    } finally { setReading(false); }
  };
  return <>
    <h1 className="vx-title">Your business</h1>
    <p className="vx-sub">Your assistant reads your website and learns your hours, services and prices.</p>
    <form onSubmit={submit}>
      <label className="vx-label">Business name
        <input className="vx-field" required maxLength={120} value={c.business ?? ""} placeholder="Maya's Nail Studio" autoComplete="organization" onChange={(e) => set({ business: e.target.value })} /></label>
      <label className="vx-label">City
        <input className="vx-field" maxLength={120} value={c.city ?? ""} placeholder="Austin, TX" autoComplete="address-level2" onChange={(e) => set({ city: e.target.value })} /></label>
      <label className="vx-label">Website
        <input className="vx-field" type="text" inputMode="url" maxLength={300} value={c.website ?? ""} placeholder="mayasnails.com" autoComplete="url" onChange={(e) => set({ website: e.target.value })} /></label>
      <label className="vx-label">Your mobile, for hand-offs and alerts
        <input className="vx-field" type="tel" required maxLength={30} value={c.handoff.number} placeholder="(415) 555-0147" autoComplete="tel" onChange={(e) => set({ handoff: { ...c.handoff, number: e.target.value } })} /></label>
      {reading ? <p className="vx-reading" role="status">Reading {c.website}. This takes about a minute.</p>
        : note && <p className="vx-save" role="status">{note}</p>}
      <Foot back={null}><Button type="submit" disabled={busy || reading}>{c.website?.trim() ? "Read my site" : "Continue"}</Button></Foot>
    </form>
  </>;
}

function Learned({ c, set, go, busy, back }: StepProps) {
  const name = nameOf(c);
  const guessed = c.services.some((s) => s.minutesGuessed);
  const fromSite = c.knowledge.some((k) => k.source === "from site") || c.services.length > 0;
  return <>
    <h1 className="vx-title">Check what your assistant learned</h1>
    <p className="vx-sub">{fromSite ? `From ${c.website}. Fix what's wrong and skip the rest.` : "Add what callers ask about most. Skip anything you're not sure of."} This is the Agent page; you can change it any time.</p>
    <div className="vx-body">
      <Sec title="Hours" hint="It answers around the clock and books only inside these hours."><HoursEditor c={c} set={set} /></Sec>
      <Sec title="Services" hint={guessed ? "Outlined durations are guesses; the site doesn't say. Check them." : undefined}><ServicesEditor c={c} set={set} /></Sec>
      <Sec title="Answers"><KnowledgeList c={c} set={set} />
        <ToAdd c={c} set={set} name={name} why="Callers usually ask these and your site doesn't say." /></Sec>
    </div>
    <Foot back={back}><Button disabled={busy} onClick={() => go(c, 3)}>Looks right</Button></Foot>
  </>;
}

function Bookings({ c, set, go, busy, back }: StepProps) {
  const { connections, startOAuth } = useConnections();
  const google = connections.find((x) => x.provider === "google");
  const [connecting, setConnecting] = useState(false);
  const connect = async () => {
    setConnecting(true);
    try { await go({ ...c, bookingsGo: "google" }, 3); await startOAuth("google"); } finally { setConnecting(false); }
  };
  const choice = (value: AgentConfig["bookingsGo"], title: string, sub: string, extra?: React.ReactNode) =>
    <label className="vx-choice">
      <input type="radio" name="bookingsGo" checked={(c.bookingsGo === "google" ? "google" : "own") === value} onChange={() => set({ bookingsGo: value })} />
      <span>{title}<small>{sub}</small></span>{extra}
    </label>;
  return <>
    <h1 className="vx-title">Where bookings go</h1>
    <p className="vx-sub">Your assistant books into the calendar you already use, during the call.</p>
    <div className="vx-body" role="radiogroup" aria-label="Where bookings go">
      {choice("google", "Google Calendar", google?.status === "connected" ? "Connected" : "Books straight into your calendar",
        google?.status === "connected" ? <span className="vx-tag" data-tone="ok">Connected</span>
          : <Button type="button" size="sm" variant="outline" disabled={connecting || !google} onClick={connect}>{connecting ? "Opening Google…" : "Connect"}</Button>)}
      {choice("own", "Just its own calendar for now", "You see every booking on Home; connect a calendar later")}
      <label className="vx-label">Buffer between appointments (minutes)
        <input className="vx-field" type="number" min={0} step={5} value={c.bufferMin} style={{ width: 120 }} onChange={(e) => set({ bufferMin: Number(e.target.value) })} /></label>
    </div>
    <Foot back={back}><Button disabled={busy} onClick={() => go(c, 4)}>Continue</Button></Foot>
  </>;
}

function NumberStep({ c, set, go, busy, back }: StepProps) {
  const api = useApi();
  const saved = c.numberChoice;
  const [path, setPath] = useState<"keep" | "new">(saved?.path ?? "keep");
  const [carrier, setCarrier] = useState<Carrier>((saved?.carrier as Carrier) || "verizon");
  const [mode, setMode] = useState<ForwardMode>(saved?.mode ?? "missed");
  const [area, setArea] = useState(saved?.areaCode ?? "");
  const [err, setErr] = useState("");
  const info = FORWARDING_CODES[carrier];
  const m = info.modes.includes(mode) ? mode : info.modes[0];
  const digits = c.number.replace(/^\+1/, "").replace(/\D/g, "");
  const codes = forwardingCodes(carrier, m, digits || "<assistant number>");
  const note = info.note?.replace("<n>", c.number ? usPhone(c.number) : "your assistant's number");
  const areaOk = /^[2-9]\d{2}$/.test(area);
  // The server keeps the choice in the document; PUT replaces the document, so merge what it returns before saving.
  const next = async () => {
    setErr("");
    try {
      const r = (await api.post("/voice/number", path === "new" ? { path, areaCode: area } : { path, carrier, mode: m })) as { numberChoice: AgentConfig["numberChoice"] };
      await go({ ...c, numberChoice: r.numberChoice }, 5);
    } catch { setErr("Couldn't save your choice. Try again."); }
  };
  const pick = (value: "keep" | "new", title: string, sub: string) =>
    <label className="vx-choice">
      <input type="radio" name="numberPath" checked={path === value} onChange={() => setPath(value)} />
      <span>{title}<small>{sub}</small></span><span />
    </label>;
  return <>
    <h1 className="vx-title">Your number</h1>
    <p className="vx-sub">Callers reach your assistant on a number. Keep yours, or get a new one.</p>
    <div className="vx-body">
      <div role="radiogroup" aria-label="Your number" style={{ display: "grid", gap: 8 }}>
        {pick("keep", "Keep my number", "Forward your calls to your assistant. Nothing to port.")}
        {pick("new", "Get a new number", "A new business line for your assistant.")}
      </div>
      {path === "keep" ? <>
        <label className="vx-label">Your carrier
          <select className="vx-field" value={carrier} onChange={(e) => setCarrier(e.target.value as Carrier)}>
            {(Object.keys(FORWARDING_CODES) as Carrier[]).map((k) => <option key={k} value={k}>{FORWARDING_CODES[k].label}</option>)}
          </select></label>
        <label className="vx-label">Your business line
          <input className="vx-field" type="tel" value={c.forwardFrom} placeholder="(415) 555-0199" onChange={(e) => set({ forwardFrom: e.target.value })} /></label>
        <div role="radiogroup" aria-label="Which calls" style={{ display: "grid", gap: 8 }}>
          {info.modes.includes("missed") && <label className="vx-choice"><input type="radio" name="fwdMode" checked={m === "missed"} onChange={() => setMode("missed")} /><span>Missed and busy calls only</span><span /></label>}
          <label className="vx-choice"><input type="radio" name="fwdMode" checked={m === "all"} onChange={() => setMode("all")} /><span>All calls</span><span /></label>
        </div>
        <div className="vx-card">
          <p className="vx-h">Forward your line</p>
          <p>{c.number ? `Your assistant's number is ${usPhone(c.number)}.` : "Your assistant's number appears at Go live."}</p>
          {codes ? <>
            <p>From your phone, dial {codes.on.length > 1 ? "these, one at a time" : "this"} and press call.</p>
            {codes.on.map((x) => <span key={x} className="vx-dial" style={{ overflowWrap: "anywhere" }}>{x}</span>)}
            <p>To stop forwarding, dial {codes.off.map((x) => <span key={x} className="vx-code">{x} </span>)}</p>
          </> : <p>{note}</p>}
          <p>{IPHONE_NOTE} {CHARGES_NOTE}</p>
        </div>
        {c.number ? <ForwardCheck c={c} set={set} /> : <p className="vx-sub">You can check your forwarding once your assistant&apos;s number is ready.</p>}
      </> : <>
        <label className="vx-label">Area code
          <input className="vx-field" inputMode="numeric" maxLength={3} value={area} placeholder="415" style={{ width: 120 }} onChange={(e) => setArea(e.target.value.replace(/\D/g, ""))} /></label>
        <p className="vx-sub">Your new business line is ready at Go live; put it on your Google Business Profile and website.</p>
      </>}
      {err && <p className="vx-save" data-err="true" role="alert">{err}</p>}
    </div>
    <Foot back={back}><Button disabled={busy || (path === "new" && !areaOk)} onClick={next}>Continue</Button></Foot>
  </>;
}

function NameStep({ c, set, go, busy, back }: StepProps) {
  const name = nameOf(c);
  return <>
    <h1 className="vx-title">Name it, then try it</h1>
    <p className="vx-sub">Callers hear this name. In a meeting you say &ldquo;{name}, …&rdquo;.</p>
    <div className="vx-body">
      <label className="vx-label">What should callers call your assistant?
        <input className="vx-field" maxLength={40} value={c.name} placeholder="Lightbulb" onChange={(e) => set({ name: e.target.value })} /></label>
      <div className="vx-chips" style={{ margin: 0 }} role="group" aria-label="Suggestions">
        {NAMES.map((n) => <button key={n} type="button" className="vx-chip" aria-pressed={name === n} onClick={() => set({ name: n })}>{n}</button>)}
      </div>
      <p className="vx-sub" style={{ marginTop: -6 }}>Not a real staff member&apos;s name: callers should know it&apos;s an assistant.</p>
      <label className="vx-label">Greeting style (optional)
        <textarea className="vx-field" rows={3} maxLength={400} value={c.greeting} placeholder={`How ${name} should open: tone, what to mention`} onChange={(e) => set({ greeting: e.target.value })} /></label>
      <p className="vx-sub" style={{ marginTop: -6 }}>{name} always says its name, that it&apos;s an AI assistant and that the call is recorded.</p>
      {c.number && <div className="vx-card">
        <p className="vx-h">Try a booking</p>
        <p>Call {usPhone(c.number)} from your mobile and book something, the way a client would. It works before you go live.</p>
        <div className="vx-actions"><Button asChild variant="outline" size="sm"><a href={`tel:${c.number}`}>Call {name} now</a></Button></div>
      </div>}
    </div>
    <Foot back={back}><Button disabled={busy} onClick={() => go({ ...c, name }, 6)}>Go live</Button></Foot>
  </>;
}
