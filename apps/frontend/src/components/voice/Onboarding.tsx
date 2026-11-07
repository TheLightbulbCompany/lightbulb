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
import { Check, HoursEditor, KnowledgeList, Sec, ServicesEditor, ToAdd, defaultGreeting, isLawful } from "./AgentPage";
import { DAYS, nameOf, usPhone, type AgentConfig } from "./data";

const NAMES = ["Nova", "Sol", "Remy", "Juno"];

interface Draft {
  read: boolean; hours: AgentConfig["hours"]; services: AgentConfig["services"]; staff: string[];
  knowledge: AgentConfig["knowledge"]; toAdd: string[]; phone: string;
}

/** Fold the website draft into the document: what the site said wins over the defaults, nothing over the owner. */
export function applyDraft(c: AgentConfig, d: Draft): AgentConfig {
  const staff = d.staff.length ? [{ q: "Who works here?", a: d.staff.join(", "), source: "from site" }] : [];
  return {
    ...c,
    hours: Object.keys(d.hours).length === DAYS.length ? d.hours : c.hours,
    services: d.services.length ? d.services : c.services,
    knowledge: [...d.knowledge, ...staff],
    toAdd: d.toAdd,
    forwardFrom: c.forwardFrom || d.phone,
  };
}

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
      const doc = to > 5 ? { ...next, live: true, paused: false, onboardingStep: undefined } : { ...next, onboardingStep: to };
      await save(doc);
      setC(doc); setStep(Math.min(to, 5));
      window.scrollTo?.({ top: 0 });
    } catch (e) {
      setErr((e as { status?: number }).status === 403 ? "Only an admin of this workspace can set up the assistant." : "Couldn't save. Try again.");
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
  const choice = (value: AgentConfig["bookingsGo"], title: string, sub: string, extra?: React.ReactNode, disabled?: boolean) =>
    <label className="vx-choice">
      <input type="radio" name="bookingsGo" checked={c.bookingsGo === value} disabled={disabled} onChange={() => set({ bookingsGo: value })} />
      <span>{title}<small>{sub}</small></span>{extra}
    </label>;
  return <>
    <h1 className="vx-title">Where bookings go</h1>
    <p className="vx-sub">Your assistant books into the calendar you already use, during the call.</p>
    <div className="vx-body" role="radiogroup" aria-label="Where bookings go">
      {choice("google", "Google Calendar", google?.status === "connected" ? "Connected" : "Books straight into your calendar",
        google?.status === "connected" ? <span className="vx-tag" data-tone="ok">Connected</span>
          : <Button type="button" size="sm" variant="outline" disabled={connecting || !google} onClick={connect}>{connecting ? "Opening Google…" : "Connect"}</Button>)}
      {choice("square", "Square Appointments", "Coming soon", undefined, true)}
      {choice("other", "Fresha, GlossGenius, Booksy or Vagaro", "It holds the slot during the call and writes the booking there after, signed in as you")}
      {c.bookingsGo === "other" && <label className="vx-label">Which one
        <input className="vx-field" value={c.otherSystem} placeholder="Fresha" onChange={(e) => set({ otherSystem: e.target.value })} /></label>}
      {choice("own", "Just its own calendar for now", "You see every booking on Home; connect a calendar later")}
      <label className="vx-label">Buffer between appointments (minutes)
        <input className="vx-field" type="number" min={0} step={5} value={c.bufferMin} style={{ width: 120 }} onChange={(e) => set({ bufferMin: Number(e.target.value) })} /></label>
    </div>
    <Foot back={back}><Button disabled={busy} onClick={() => go(c, 4)}>Continue</Button></Foot>
  </>;
}

function NumberStep({ c, set, go, busy, back }: StepProps) {
  const api = useApi();
  const [state, setState] = useState<"loading" | "ok" | "taken" | "not_available" | "error">(c.number ? "ok" : "loading");
  useEffect(() => {
    if (c.number) return;
    api.post("/voice/number", {}).then(
      (r) => { set({ number: (r as { number: string }).number }); setState("ok"); },
      (e: { status?: number; body?: { detail?: { code?: string } } }) =>
        setState(e?.status === 409 ? (e.body?.detail?.code === "taken" ? "taken" : "not_available") : "error"),
    );
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const shown = c.number ? usPhone(c.number) : "";
  const dial = c.number.replace(/^\+1/, "").replace(/\D/g, "").replace(/(\d{3})(\d{3})(\d{4})/, "$1 $2 $3");
  return <>
    <h1 className="vx-title">Your number</h1>
    <p className="vx-sub">Keep your number. Forward it to your assistant&apos;s line; nothing to port.</p>
    <div className="vx-body">
      {state === "loading" && <p className="vx-reading" role="status">Getting your assistant a number…</p>}
      {state === "ok" && <>
        <div><p className="vx-label">Your assistant&apos;s number</p><p className="vx-number">{shown}</p></div>
        <label className="vx-label">Your business line
          <input className="vx-field" type="tel" value={c.forwardFrom} placeholder="(415) 555-0199" onChange={(e) => set({ forwardFrom: e.target.value })} /></label>
        <div className="vx-card">
          <p className="vx-h">Forward your line</p>
          <p>From your business phone, dial this and press call. Most carriers confirm with a beep.</p>
          <span className="vx-dial">*72 {dial}</span>
          <p>To stop forwarding, dial <span className="vx-code">*73</span>. Google Voice, VoIP or a carrier that doesn&apos;t take <span className="vx-code">*72</span>: turn on call forwarding in its settings and enter {shown}.</p>
        </div>
        <p className="vx-sub">Checking that it forwards: once forwarding checks are on, we&apos;ll ring your line here and you&apos;ll see it arrive.</p>
      </>}
      {(state === "taken" || state === "not_available" || state === "error") && <div className="vx-card" role="status">
        <p className="vx-h">{state === "error" ? "Couldn't get a number just now" : "No number to give out here yet"}</p>
        <p>{state === "taken" ? "This test environment has one number and another workspace is using it." : state === "error" ? "Try again later from the Agent page." : "Numbers are being set up. Finish the rest now; your number arrives on the Agent page."}</p>
      </div>}
    </div>
    <Foot back={back}>
      {state === "ok" ? <><Button variant="ghost" disabled={busy} onClick={() => go(c, 5)}>Skip, I&apos;ll forward later</Button>
        <Button disabled={busy} onClick={() => go(c, 5)}>I&apos;ve forwarded it</Button></>
        : <Button disabled={busy || state === "loading"} onClick={() => go(c, 5)}>Continue</Button>}
    </Foot>
  </>;
}

function NameStep({ c, set, go, busy, back }: StepProps) {
  const business = c.business || "your business";
  const name = c.name.trim() || "Nova";
  const greeting = c.greeting || defaultGreeting(business, name);
  const lawful = isLawful(greeting);
  return <>
    <h1 className="vx-title">Name it, then try it</h1>
    <p className="vx-sub">Callers hear this name. In a meeting you say &ldquo;{name}, …&rdquo;.</p>
    <div className="vx-body">
      <label className="vx-label">What should callers call your assistant?
        <input className="vx-field" maxLength={40} value={c.name} placeholder="Nova" onChange={(e) => set({ name: e.target.value })} /></label>
      <div className="vx-chips" style={{ margin: 0 }} role="group" aria-label="Suggestions">
        {NAMES.map((n) => <button key={n} type="button" className="vx-chip" aria-pressed={name === n} onClick={() => set({ name: n })}>{n}</button>)}
      </div>
      <p className="vx-sub" style={{ marginTop: -6 }}>Not a real staff member&apos;s name: callers should know it&apos;s an assistant.</p>
      <label className="vx-label">Greeting
        <textarea className="vx-field" rows={3} maxLength={400} value={greeting} onChange={(e) => set({ greeting: e.target.value })} /></label>
      {!lawful && <p className="vx-save" data-err="true">Keep &ldquo;AI assistant&rdquo; and &ldquo;this call is recorded&rdquo; in the greeting: the recording and AI-disclosure laws want both.</p>}
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "end" }}>
        <label className="vx-label">Voice
          <select className="vx-field" value={c.voice} onChange={(e) => set({ voice: e.target.value })}>
            {["Warm", "Bright", "Calm"].map((v) => <option key={v}>{v}</option>)}</select></label>
        <Check label="Also speaks Spanish" on={c.spanish} set={(spanish) => set({ spanish })} />
      </div>
      {c.number && <div className="vx-card">
        <p className="vx-h">Try a booking</p>
        <p>Call {usPhone(c.number)} from your mobile and book something, the way a client would. It works before you go live.</p>
        <div className="vx-actions"><Button asChild variant="outline" size="sm"><a href={`tel:${c.number}`}>Call {name} now</a></Button></div>
      </div>}
    </div>
    <Foot back={back}><Button disabled={busy || !lawful} onClick={() => go({ ...c, name, greeting }, 6)}>Go live</Button></Foot>
  </>;
}
