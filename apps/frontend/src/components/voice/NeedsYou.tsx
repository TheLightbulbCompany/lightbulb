"use client";

// Needs you (spec 2026-11-07 §5): one list, the kind label on each card, the action on the card. One source: the open
// Questions (/needs-you), calls included: a call's "didn't know" and "wanted a person" are Questions (voice_phone.call_ask).
import { useState } from "react";
import Link from "next/link";
import type { QuestionView } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { LIGHTBULB, useAgentConfig, useQuestions, useTimeline } from "./data";

// #5043: GET /needs-you adds pendingConfirm when a voice yes is waiting for the tap. Local type: packages/shared is not touched (mobile OTA).
type Q = QuestionView & { pendingConfirm?: { by: string; seat: "meet" | "app" | "phone_owner"; roomId?: string; at: string } | null };

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
const time = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

export function NeedsYou() {
  const { data: qs, error: qErr } = useQuestions();
  const { data: items, error: tErr } = useTimeline();
  const name = LIGHTBULB;
  const total = qs?.length ?? 0;
  const today = (items ?? []).filter((i) => new Date(i.at).toDateString() === new Date().toDateString());
  const loading = !qs && !qErr || !items && !tErr;
  return <div className="vx-page">
    <h1 className="vx-title">{loading ? "Needs you" : total ? `${total} ${total === 1 ? "needs" : "need"} you` : "Nothing needs you"}</h1>
    <p className="vx-sub">Only what a person must decide lands here. Answers go into Knowledge, so the same question doesn&apos;t come back.</p>
    {(qErr || tErr) && <p className="vx-empty" role="alert">Couldn&apos;t load everything. What loaded is below.</p>}
    <div className="vx-cards">
      {qs?.map((q) => <QuestionCard key={q.id} q={q} name={name} />)}
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

function QuestionCard({ q, name }: { q: Q; name: string }) {
  const api = useApi();
  const { mutate } = useQuestions();
  const remember = useKnowledge();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const ok = q.kind !== "question";
  // #5028: a call's Question carries subkind didnt_know | handoff | line_problem and preview {caller, question}.
  const label = ok ? "Needs your OK" : q.subkind === "handoff" ? "Caller wanted a person"
    : q.subkind === "line_problem" ? "Line problem" : `${name} didn't know`;
  const caller = (q.preview as { caller?: string } | null | undefined)?.caller;
  const google = q.subkind === "line_problem" && /google|calendar/i.test(`${q.title} ${q.text}`);
  // A call's "didn't know": the answer goes into Knowledge server-side (routers/questions.py), for any answerer.
  const answers = q.options.some((o) => o.id === "answer");
  const send = async (optionId: string, typed?: string) => {
    setBusy(true); setErr(null);
    try {
      await api.post(`/tasks/${encodeURIComponent(q.taskId)}/questions/${encodeURIComponent(q.id)}/answer`,
        { optionId, clientAnswerId: crypto.randomUUID(), expectVersion: q.version, ...(typed ? { text: typed } : {}) });
      if (typed && !ok && q.subkind !== "didnt_know") await remember(q.title, typed);
      setDone(true); void mutate();
    } catch (e) {
      const code = (e as { body?: { detail?: { code?: string } } }).body?.detail?.code;
      setErr(code === "not_open" ? "Someone already answered this." : code === "not_answerer" ? "This one is waiting on someone else." : "Couldn't send. Try again.");
    } finally { setBusy(false); }
  };
  // An approval card's first option is the approve one; with a voice yes waiting it becomes the primary "Confirm".
  const pc = ok ? q.pendingConfirm : null;
  const option = (o: QuestionView["options"][number]) => o.id === "call_myself" && caller
    ? <Button key={o.id} asChild size="sm" variant="outline"><a href={`tel:${caller}`}>{o.label}</a></Button>
    : <Button key={o.id} type="button" size="sm" disabled={busy}
      variant={o.style === "primary" || pc && o === q.options[0] ? "default" : "outline"}
      onClick={() => void send(o.id)}>{pc && o === q.options[0] ? "Confirm" : o.label}</Button>;
  return <article className="vx-card" data-done={done}>
    <p className="vx-kind"><b>{label}</b> · {time(Date.parse(q.createdAt))}</p>
    <h2>{q.title}</h2>
    {pc && <p className="vx-kind">{pc.seat === "meet" ? "Approved by voice in a meeting" : "You said yes"} · {time(Date.parse(pc.at))}</p>}
    {q.text && q.text !== q.title && <p style={{ whiteSpace: "pre-wrap" }}>{q.text}</p>}
    {done ? <p>Done.</p> : !q.canAnswer ? <p>{q.waitingLabel}</p> : q.options.length && !answers
      ? <div className="vx-actions">
        {google && <Button asChild size="sm"><Link href="/voice/settings#connections">Reconnect Google</Link></Button>}
        {q.options.map(option)}</div>
      : <form className="vx-grid" onSubmit={(e) => { e.preventDefault(); if (text.trim()) void send("answer", text.trim()); }}>
        <textarea className="vx-field" rows={2} maxLength={4000} value={text} onChange={(e) => setText(e.target.value)} aria-label="Your answer" />
        <div className="vx-actions" style={{ marginTop: 0 }}><Button type="submit" size="sm" disabled={busy || !text.trim()}>Answer</Button>
          {q.options.filter((o) => o.id !== "answer").map((o) => option({ ...o, style: "secondary" }))}</div>
      </form>}
    {err && <p role="alert" style={{ color: "var(--err)" }}>{err}</p>}
  </article>;
}
