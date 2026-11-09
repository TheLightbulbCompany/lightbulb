"use client";

// Home, "Lightbulb-first" (spec 2026-11-07 §5, mockup D): tap to talk to the assistant, the one Needs-you line, then
// one timeline of every call and meeting with what the assistant did, and the pane with how it did it.
import { useMemo, useState } from "react";
import Link from "next/link";
import { meetingStatus } from "@/lib/meetingStatus";
import { ArrowLeft, Phone, UserRound, Video } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TalkOrb } from "./TalkOrb";
import { Onboarding } from "./Onboarding";
import { useApi } from "@/lib/api";
import { useNeedsYouTotal } from "./VoiceShell";
import {
  callNeedsYou, callSummary, mmss, monthMinutes, LIGHTBULB, needsOnboarding, useAgentConfig, useTimeline, who,
  type CallItem, type MeetingItem, type Outcome, type TimelineItem,
} from "./data";

const OUTCOME: Record<Outcome, [string, "ok" | "warn" | "info" | undefined]> = {
  booked: ["Booked", "ok"], moved: ["Moved", "ok"], cancelled: ["Cancelled", undefined], message: ["Message", "info"],
  answered: ["Answered", undefined], needs_you: ["Needs you", "warn"], handoff_missed: ["Wanted you", "warn"], spam: ["Spam", undefined],
  silent: ["Silent", undefined], forwarded: ["Forwarded", "info"],
};

export function Tag({ outcome }: { outcome?: Outcome }) {
  const [label, tone] = OUTCOME[outcome ?? "answered"] ?? OUTCOME.answered;
  return <span className="vx-tag" data-tone={tone}>{label}</span>;
}

const time = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
function dayLabel(ms: number): string {
  const d = new Date(ms); const today = new Date();
  const days = Math.round((new Date(today.toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 864e5);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
}

type Filter = "all" | "calls" | "meetings" | "yours" | "needs";
const FILTERS: [Filter, string, (i: TimelineItem) => boolean][] = [
  ["all", "All", () => true],
  ["calls", "Calls", (i) => i.type === "call" && !i.ownerMode],
  ["meetings", "Meetings", (i) => i.type === "meeting"],
  ["yours", "Yours", (i) => i.type === "call" && !!i.ownerMode],
  ["needs", "Needs you", callNeedsYou],
];

export function VoiceHome() {
  const { data: items, error, isLoading } = useTimeline();
  const { data: config, isNew, save } = useAgentConfig();
  const name = LIGHTBULB;
  const needs = useNeedsYouTotal();
  const [filter, setFilter] = useState<Filter>("all");
  const [now] = useState(() => Date.now());  // one clock per render tree: "Upcoming" is judged against this
  const [openId, setOpenId] = useState<string | null>(null);
  const all = useMemo(() => items ?? [], [items]);
  const shown = all.filter(FILTERS.find((f) => f[0] === filter)![2]);
  const open = all.find((i) => i.roomId === openId) ?? null;
  const groups = useMemo(() => {
    const g: [string, TimelineItem[]][] = [];
    for (const i of shown) { const d = dayLabel(i.at); if (g.at(-1)?.[0] === d) g.at(-1)![1].push(i); else g.push([d, [i]]); }
    return g;
  }, [shown]);
  const today = all.filter((i) => dayLabel(i.at) === "Today" && i.type === "call");
  const minutes = monthMinutes(all);

  if (config && needsOnboarding(config, isNew)) return <Onboarding config={config} save={save} />;
  return <div className="vx-home" data-pane={!!open}>
    <div className="vx-list"><div className="vx-page">
      <TalkOrb name={name} />
      {needs > 0 && <Link className="vx-needs" href="/voice/needs-you">
        <span><span className="vx-dot" aria-hidden="true" />{needs} {needs === 1 ? "thing needs" : "things need"} you</span>
        <span className="vx-muted">Open</span>
      </Link>}
      <div className="vx-chips" role="group" aria-label="Show">
        {FILTERS.map(([id, label, test]) => {
          // "Needs you" filters the calls; the line above counts everything waiting (calls and Questions), so no number here.
          const n = id === "all" || id === "needs" ? 0 : all.filter(test).length;
          return <button key={id} type="button" className="vx-chip" aria-pressed={filter === id} onClick={() => setFilter(id)}>
            {label}{n > 0 && <span>{n}</span>}</button>;
        })}
      </div>
      {error && <p className="vx-empty" role="alert">Couldn&apos;t load the calls. They&apos;ll show when the connection is back.</p>}
      {!error && isLoading && <p className="vx-empty">Loading calls and meetings…</p>}
      {!error && !isLoading && shown.length === 0 && <p className="vx-empty">{filter === "all"
        ? `No calls or meetings yet. When ${name} answers your line or joins a Meet, every conversation shows here with what it did.`
        : "Nothing here yet."}</p>}
      {groups.map(([day, rows]) => <section key={day} aria-label={day}>
        <h2 className="vx-day">{day}</h2>
        {rows.map((i) => <Row key={i.roomId} item={i} name={name} now={now} current={i.roomId === openId} onOpen={() => setOpenId(i.roomId)} />)}
      </section>)}
      {all.length > 0 && <p className="vx-foot">
        Today {today.length} answered · {today.filter((c) => c.type === "call" && c.outcome === "booked").length} booked
        {" "}&nbsp;·&nbsp; {minutes} min in this month&apos;s call records</p>}
    </div></div>
    {open && <aside className="vx-pane" aria-label="Details">
      <Button type="button" variant="ghost" size="sm" className="vx-back" onClick={() => setOpenId(null)}><ArrowLeft /> All conversations</Button>
      {open.type === "call" ? <CallPane call={open} name={name} onClose={() => setOpenId(null)} /> : <MeetingPane meeting={open} name={name} onClose={() => setOpenId(null)} />}
    </aside>}
  </div>;
}

function Row({ item, name, current, now, onOpen }: { item: TimelineItem; name: string; current: boolean; now: number; onOpen: () => void }) {
  if (item.type === "meeting") {
    const presentation = meetingStatus(item, now);
    const mins = item.endAt && item.startAt ? Math.round((item.endAt - item.startAt) / 60) : null;
    const did = item.summary || [item.asked?.length && `Asked ${item.asked.length === 1 ? "once" : `${item.asked.length} times`}`,
      item.followUps?.length && `${item.followUps.length} follow-up${item.followUps.length === 1 ? "" : "s"}`].filter(Boolean).join(" · ")
      || presentation.detail;
    return <button type="button" className="vx-row" aria-current={current} onClick={onOpen}>
      <time>{time(item.at)}</time><Video size={16} aria-label="Meeting" />
      <span><span className="vx-who">{item.title || "Meeting"}<small>Google Meet{item.people?.length ? ` · ${item.people.join(", ")}` : ""}</small></span>
        <span className="vx-did"><span className="vx-tag">{presentation.label}</span><span>{did}</span></span></span>
      <span className="vx-dur">{mins != null ? `${mins} min` : ""}</span>
    </button>;
  }
  const Icon = item.ownerMode ? UserRound : Phone;
  const sub = item.ownerMode ? "you" : [item.callerName && item.caller, item.returning === true ? "returning" : item.returning === false ? "new caller" : null].filter(Boolean).join(" · ");
  return <button type="button" className="vx-row" aria-current={current} onClick={onOpen}>
    <time>{time(item.at)}</time><Icon size={16} aria-label={item.ownerMode ? "You" : "Call"} />
    <span><span className="vx-who">{item.ownerMode ? `You and ${name}` : who(item)}{sub && !item.ownerMode && <small>{sub}</small>}</span>
      <span className="vx-did"><Tag outcome={item.outcome} /><span>{callSummary(item)}</span></span></span>
    <span className="vx-dur">{mmss(item.durationSec)}</span>
  </button>;
}

function ReachBack({ number }: { number?: string }) {
  if (!number) return null;
  return <><Button asChild size="sm" variant="outline"><a href={`tel:${number}`}>Call back</a></Button>
    <Button asChild size="sm" variant="outline"><a href={`sms:${number}`}>Text back</a></Button></>;
}

function CallPane({ call, name, onClose }: { call: CallItem; name: string; onClose: () => void }) {
  const [all, setAll] = useState(false);
  const [forget, setForget] = useState<"idle" | "busy" | "gone" | "error">("idle");
  const api = useApi();
  const { mutate } = useTimeline();
  const tx = call.transcript ?? [];
  const forgetCaller = async () => {
    if (!call.caller || !(await window.confirm(`Forget what ${name} remembers about ${call.callerName || "this caller"}? This can't be undone.`))) return;
    setForget("busy");
    try { await api.del(`/voice/callers/${encodeURIComponent(call.caller)}/memory`); setForget("gone"); void mutate(); }
    catch { setForget("error"); }
  };
  return <>
    <div className="vx-pane-head">
      <div><h2 className="vx-title" style={{ fontSize: "var(--text-ui)" }}>{call.ownerMode ? `You and ${name}` : who(call)}</h2>
        <p className="vx-sub">{time(call.at)} · {dayLabel(call.at)}{call.durationSec ? ` · ${mmss(call.durationSec)}` : ""}</p></div>
      <Button type="button" size="sm" variant="ghost" className="vx-close" onClick={onClose} aria-label="Close details">Close</Button>
    </div>
    <p style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}><Tag outcome={call.outcome} />
      <span className="vx-muted" style={{ fontSize: "var(--text-caption)" }}>{call.booking || call.summary}</span></p>
    {!call.ownerMode && <div className="vx-actions"><ReachBack number={call.caller} /></div>}
    {!!call.steps?.length && <section><h3>What {name} did</h3>
      <ol className="vx-steps">{call.steps.map((s, n) => <li key={n}><time>{mmss(s.t)}</time><span>{s.text}
        {s.guessed && <> <span className="vx-tag" data-tone="warn">guessed, not in Knowledge</span></>}</span></li>)}</ol></section>}
    {!!call.callerMemory?.length && forget !== "gone" && <section><h3>What {name} remembers about {call.callerName || "this caller"}</h3>
      <ul className="vx-tx">{call.callerMemory.map((m) => <li key={m}>{m}</li>)}</ul>
      <div className="vx-actions" style={{ alignItems: "center" }}>
        <Button type="button" size="sm" variant="outline" disabled={forget === "busy"} onClick={forgetCaller}>Forget</Button>
        <span className="vx-save" role="status" data-err="true">{forget === "error" ? "Couldn't forget. Try again." : ""}</span></div></section>}
    {tx.length > 0 && <section><h3>Transcript</h3>
      <div className="vx-tx">{(all ? tx : tx.slice(0, 6)).map((l, n) => <p key={n}><b>{l.who === "assistant" ? name : call.ownerMode ? "You" : "Caller"}:</b> {l.text}</p>)}</div>
      {tx.length > 6 && <Button type="button" size="sm" variant="ghost" onClick={() => setAll(!all)}>{all ? "Show less" : `Show all ${tx.length} lines`}</Button>}
    </section>}
    {call.recordingUrl && <section><h3>Recording</h3><audio controls preload="none" src={call.recordingUrl} style={{ width: "100%" }} /></section>}
    {!call.ownerMode && <NextTime roomId={call.roomId} />}
  </>;
}

/** "Next time…": a note the owner writes on a call, saved into Knowledge so the assistant says it next time. */
function NextTime({ roomId }: { roomId: string }) {
  const { data: config, save } = useAgentConfig();
  const [text, setText] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error" | "forbidden">("idle");
  return <section key={roomId}><h3>Next time…</h3>
    <form onSubmit={async (e) => {
      e.preventDefault();
      if (!config || !text.trim()) return;
      setState("saving");
      const when = new Date().toLocaleDateString([], { day: "numeric", month: "short" });
      try {
        await save({ ...config, knowledge: [...config.knowledge, { q: "", a: text.trim(), source: `added from a call, ${when}` }] });
        setText(""); setState("saved");
      } catch (err) { setState((err as { status?: number }).status === 403 ? "forbidden" : "error"); }
    }}>
      <textarea className="vx-field" rows={2} maxLength={1000} value={text} onChange={(e) => setText(e.target.value)}
        aria-label="What should it say next time" placeholder="Parking is behind the building, not on the street" />
      <div className="vx-actions" style={{ alignItems: "center" }}>
        <Button type="submit" size="sm" disabled={!text.trim() || state === "saving"}>Save to Knowledge</Button>
        <span className="vx-save" role="status" data-err={state === "error" || state === "forbidden"}>
          {state === "saved" ? "Saved. It uses this from the next call." : state === "error" ? "Couldn't save. Try again."
            : state === "forbidden" ? "Only an admin can change Knowledge." : ""}</span>
      </div>
    </form>
  </section>;
}

function MeetingPane({ meeting, name, onClose }: { meeting: MeetingItem; name: string; onClose: () => void }) {
  const presentation = meetingStatus(meeting);
  return <>
    <div className="vx-pane-head">
      <div><h2 className="vx-title" style={{ fontSize: "var(--text-ui)" }}>{meeting.title || "Meeting"}</h2>
        <p className="vx-sub">{time(meeting.at)} · {dayLabel(meeting.at)}{meeting.people?.length ? ` · ${meeting.people.join(", ")}` : ""}</p></div>
      <Button type="button" size="sm" variant="ghost" className="vx-close" onClick={onClose} aria-label="Close details">Close</Button>
    </div>
    <p className="vx-tx"><strong>{presentation.label}</strong> · {presentation.detail}</p>
    {meeting.summary && <section><h3>Notes</h3><p className="vx-tx">{meeting.summary}</p></section>}
    <section><h3>What {name} was asked and did</h3>
      {meeting.asked?.length
        ? <ol className="vx-steps">{meeting.asked.map((a, n) => <li key={n}><time>{mmss(a.t)}</time>
          <span>{a.who ? `${a.who}: ` : ""}&ldquo;{a.q}&rdquo;{a.a ? ` → ${a.a}` : ""}</span></li>)}</ol>
        : <p className="vx-tx vx-muted">No questions recorded for this meeting.</p>}
    </section>
    {!!meeting.followUps?.length && <section><h3>Follow-ups</h3>
      <ul className="vx-tx">{meeting.followUps.map((f, n) => <li key={n} style={{ display: "flex", gap: 8 }}>
        <span className="vx-tag" data-tone={f.status === "done" ? "ok" : f.status === "needs_you" ? "warn" : undefined}>{f.status === "done" ? "Done" : f.status === "needs_you" ? "Needs you" : "Working"}</span>{f.text}</li>)}</ul>
    </section>}
  </>;
}
