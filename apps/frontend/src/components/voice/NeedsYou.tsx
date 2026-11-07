"use client";

// Needs you (spec 2026-11-07 §5): one list, the kind label on each card, the action on the card. Sources: the open
// Questions (/needs-you: "Needs your OK" and "didn't know" asks from tasks) and the calls still waiting on the owner.
import { useState } from "react";
import Link from "next/link";
import type { QuestionView } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { callNeedsYou, nameOf, useAgentConfig, useQuestions, useTimeline, who, type CallItem } from "./data";

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
const time = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

export function NeedsYou() {
  const { data: qs, error: qErr } = useQuestions();
  const { data: items, error: tErr } = useTimeline();
  const { data: config } = useAgentConfig();
  const name = nameOf(config);
  const calls = (items ?? []).filter(callNeedsYou);
  const total = (qs?.length ?? 0) + calls.length;
  const today = (items ?? []).filter((i) => new Date(i.at).toDateString() === new Date().toDateString());
  const loading = !qs && !qErr || !items && !tErr;
  return <div className="vx-page">
    <h1 className="vx-title">{loading ? "Needs you" : total ? `${total} ${total === 1 ? "needs" : "need"} you` : "Nothing needs you"}</h1>
    <p className="vx-sub">Only what a person must decide lands here. Answers go into Knowledge, so the same question doesn&apos;t come back.</p>
    {(qErr || tErr) && <p className="vx-empty" role="alert">Couldn&apos;t load everything. What loaded is below.</p>}
    <div className="vx-cards">
      {qs?.map((q) => <QuestionCard key={q.id} q={q} name={name} />)}
      {calls.map((c) => <CallCard key={c.roomId} call={c} name={name} />)}
    </div>
    {!loading && <p className="vx-foot" style={{ padding: 0, marginTop: 20 }}>
      {total ? "Nothing else needs you. " : ""}
      {plural(today.filter((i) => i.type === "call").length, "call")} and {plural(today.filter((i) => i.type === "meeting").length, "meeting")} handled today.{" "}
      <Link className="vx-link" href="/voice">See Home</Link></p>}
  </div>;
}

function useKnowledge() {
  const { data: config, save } = useAgentConfig();
  /** Best effort: a member can answer but only an admin edits the Agent page, so a 403 here is not the answer failing. */
  return async (q: string, a: string) => {
    if (!config) return;
    await save({ ...config, knowledge: [...config.knowledge, { q, a, source: "you answered this in Needs you" }],
      toAdd: config.toAdd.filter((t) => t !== q) }).catch(() => {});
  };
}

function QuestionCard({ q, name }: { q: QuestionView; name: string }) {
  const api = useApi();
  const { mutate } = useQuestions();
  const remember = useKnowledge();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const ok = q.kind !== "question";
  const send = async (optionId: string, typed?: string) => {
    setBusy(true); setErr(null);
    try {
      await api.post(`/tasks/${encodeURIComponent(q.taskId)}/questions/${encodeURIComponent(q.id)}/answer`,
        { optionId, clientAnswerId: crypto.randomUUID(), expectVersion: q.version, ...(typed ? { text: typed } : {}) });
      if (typed && !ok) await remember(q.title, typed);
      setDone(true); void mutate();
    } catch (e) {
      const code = (e as { body?: { detail?: { code?: string } } }).body?.detail?.code;
      setErr(code === "not_open" ? "Someone already answered this." : code === "not_answerer" ? "This one is waiting on someone else." : "Couldn't send. Try again.");
    } finally { setBusy(false); }
  };
  return <article className="vx-card" data-done={done}>
    <p className="vx-kind"><b>{ok ? "Needs your OK" : `${name} didn't know`}</b> · {time(Date.parse(q.createdAt))}</p>
    <h2>{q.title}</h2>
    {q.text && q.text !== q.title && <p style={{ whiteSpace: "pre-wrap" }}>{q.text}</p>}
    {done ? <p>Done.</p> : !q.canAnswer ? <p>{q.waitingLabel}</p> : q.options.length
      ? <div className="vx-actions">{q.options.map((o) => <Button key={o.id} type="button" size="sm" disabled={busy}
        variant={o.style === "primary" ? "default" : "outline"} onClick={() => void send(o.id)}>{o.label}</Button>)}</div>
      : <form className="vx-grid" onSubmit={(e) => { e.preventDefault(); if (text.trim()) void send("answer", text.trim()); }}>
        <textarea className="vx-field" rows={2} maxLength={4000} value={text} onChange={(e) => setText(e.target.value)} aria-label="Your answer" />
        <div><Button type="submit" size="sm" disabled={busy || !text.trim()}>Answer</Button></div>
      </form>}
    {err && <p role="alert" style={{ color: "var(--err)" }}>{err}</p>}
  </article>;
}

function CallCard({ call, name }: { call: CallItem; name: string }) {
  const api = useApi();
  const { mutate } = useTimeline();
  const remember = useKnowledge();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const handoff = call.outcome === "handoff_missed";
  const finish = async (answer?: string) => {
    setBusy(true); setErr(null);
    try {
      if (answer) await remember(call.summary || "A caller's question", answer);
      await api.post(`/voice/calls/${encodeURIComponent(call.roomId)}/done`, {});
      void mutate();
    } catch { setErr("Couldn't mark it done. Try again."); } finally { setBusy(false); }
  };
  const caller = `${who(call)}${call.returning === false ? ", new caller" : call.returning ? ", returning" : ""}`;
  return <article className="vx-card">
    <p className="vx-kind"><b>{handoff ? "Caller wanted a person" : `${name} didn't know`}</b> · call · {time(call.at)}</p>
    <h2>{call.summary || (handoff ? `${who(call)} asked for you` : `${who(call)} asked something ${name} couldn't answer`)}</h2>
    <p>{caller}. {name} took a message{handoff ? " because nobody picked up" : ""}.</p>
    {!handoff && <form className="vx-grid" style={{ marginTop: 10 }} onSubmit={(e) => { e.preventDefault(); if (text.trim()) void finish(text.trim()); }}>
      <textarea className="vx-field" rows={2} maxLength={1000} value={text} onChange={(e) => setText(e.target.value)}
        aria-label="The answer" placeholder={`The answer, so ${name} knows next time`} />
      <div className="vx-actions" style={{ marginTop: 0 }}>
        <Button type="submit" size="sm" disabled={busy || !text.trim()}>Save answer</Button>
        {call.caller && <><Button asChild size="sm" variant="outline"><a href={`tel:${call.caller}`}>I&apos;ll call</a></Button>
          <Button asChild size="sm" variant="outline"><a href={`sms:${call.caller}`}>Text back</a></Button></>}
        <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void finish()}>Done without an answer</Button>
      </div>
    </form>}
    {handoff && <div className="vx-actions">
      {call.caller && <><Button asChild size="sm" variant="outline"><a href={`tel:${call.caller}`}>Call back</a></Button>
        <Button asChild size="sm" variant="outline"><a href={`sms:${call.caller}`}>Text back</a></Button></>}
      <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void finish()}>Done</Button>
    </div>}
    {err && <p role="alert" style={{ color: "var(--err)" }}>{err}</p>}
  </article>;
}
