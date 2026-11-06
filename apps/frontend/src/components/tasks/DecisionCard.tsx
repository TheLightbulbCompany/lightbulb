"use client";

// The one "Needs you" decision card (UX audit slice 2): the same frame in the task pane, on the task card in a
// conversation and in Activity's Needs you block; the phone's NeedsYouCard is its twin over the same shared helpers.
// Who asks and when, the question (title first), its context, the exact target, then Deny / Allow once / Always allow.
// No status word: the card itself means "needs you".

import { useId, useState, type ReactElement } from "react";
import { FREE_TEXT_OPTION, askFields, askLine, decisionButtons, decisionMeta, previewRows, roomDisplayName, type QuestionView, type TaskSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { useWorkspaceState } from "@/components/workspace/workspaceController";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { cn } from "@/lib/utils";
import { useTaskActions } from "./useTaskActions";

export const endedLabel = (kind: string) => kind === "approval" ? "Closed — not approved" : kind === "review" ? "Closed — not sent" : "Closed — not answered";

/** Past this the body box clamps to 3 lines behind "Show all".
 *  ponytail: a length/line heuristic, not a measured overflow; measure the box if short-but-wide bodies clamp badly. */
const LONG_BODY = 200;

const FIELD = "w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-ui focus-visible:outline-2 focus-visible:outline-ring";

/** Only an answerer gets buttons; everyone else reads who it waits on. Typed text never answers.
 *  `ended`: the task is Closed, so the question can't be answered any more (dev walk N-F1): read-only, saying what
 *  became of it. Request changes asks what should change before it sends (N-F2).
 *  `compact` (task card, Activity): three preview rows cut to one line each, and "Open task" for the rest. */
export function DecisionCard({ task, question, ended = false, compact = false, framed = true, onOpen }: {
  task: TaskSummary; question: QuestionView; ended?: boolean; compact?: boolean;
  /** False inside a task card: a hairline under the card's own header instead of a card in a card. */
  framed?: boolean; onOpen?: () => void;
}): ReactElement {
  const { answer } = useTaskActions(compact ? "card" : "pane");
  const { agentNames } = useLiveEvents();
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);
  const [text, setText] = useState("");
  // Request changes: the reason first, then the one press (it goes to the specialist with the answer).
  const [changes, setChanges] = useState<string | null>(null);
  const headingId = `needs-${question.id}`;
  const reasonId = useId();
  // The age is read once when the card mounts (a card is answered within minutes; its meta line needs no ticking).
  const [now] = useState(() => Date.now());
  const waiting = question.waitingLabel ?? `Waiting on ${question.answerers.map((a) => a.name).join(" or ")}`;
  const heading = askLine(question, task.specialist, agentNames);
  // A Worker's own email ask carries no preview, only "Recipient: … Subject: … Body: …" text: the same rows from it.
  const parsed = question.preview ? null : askFields(question.text);
  const shownText = parsed ? parsed.intro : question.text;
  const allRows = previewRows(question.preview ?? parsed?.preview, Infinity);
  const body = allRows.find((r) => r.label === "Message");
  const rows = allRows.filter((r) => r !== body).slice(0, compact ? 3 : undefined);
  const [bodyOpen, setBodyOpen] = useState(false);
  const clampBody = !!body && !bodyOpen && (body.value.length > LONG_BODY || body.value.split("\n").length > 3);
  // The project, when the card is read outside it (Activity, Home, another project's chat).
  const directory = useMessagingWorkspaceOptional()?.directoryProps;
  const { place } = useWorkspaceState();
  const roomId = task.audience?.roomId ?? null;
  const projectRoom = roomId && (place !== "chat" || directory?.currentRoomId !== roomId)
    ? directory?.rooms.find((r) => r.roomId === roomId && r.kind === "channel") : undefined;
  const project = projectRoom ? `#${roomDisplayName(projectRoom, projectRoom.participants)}` : null;
  const buttons = decisionButtons(question.options, question.subkind, question.preview);
  const unavailable = buttons.find((b) => b.disabledReason)?.disabledReason;
  const send = async (optionId: string, typed?: string) => {
    setPending(true);
    const res = await answer(task, question.id, optionId, { expectVersion: question.version, ...(typed ? { text: typed } : {}) });
    setPending(false);
    if (res.ok) setClosed(true);
    else { setNote(res.message); if (res.answeredBy) setClosed(true); }
  };
  const live = question.canAnswer && !closed && !ended && question.state === "open";
  const meta = ended ? endedLabel(question.kind) : question.canAnswer ? decisionMeta(question, task.specialist, agentNames, now, project) : waiting;
  return <section role="group" aria-labelledby={headingId} data-decision-card={question.id}
    className={framed ? `editorial-card border-l-2 px-3 py-2 ${ended ? "border-l-border" : "border-l-warn"}` : "border-t border-border pt-2"}>
    <p className="text-caption text-muted-foreground">{meta}</p>
    <h4 id={headingId} className="mt-0.5 font-semibold text-pretty">{heading}</h4>
    {shownText && shownText !== heading && <p className={cn("mt-0.5 text-ui text-muted-foreground", compact ? "line-clamp-2" : "whitespace-pre-wrap")}>{shownText}</p>}
    {(rows.length > 0 || body) && <div className="mt-2 border-t border-border pt-2 text-ui">
      {rows.length > 0 && <dl aria-label="What this does" className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        {rows.map((r) => <div key={r.label} className="contents">
          <dt className="text-muted-foreground">{r.label}</dt>
          <dd className={cn("min-w-0", compact ? "truncate" : "whitespace-pre-wrap break-words")}>{r.value}</dd>
        </div>)}
      </dl>}
      {/* The body in a quiet box under To and Subject, clamped to 3 lines when long (W13-4). Text, never HTML. */}
      {body && <>
        <p className={cn("whitespace-pre-wrap break-words rounded-md bg-muted px-3 py-2", rows.length > 0 && "mt-2", clampBody && "line-clamp-3")}>
          <span className="sr-only">{body.label}: </span>{body.value}</p>
        {(clampBody || bodyOpen) && <button type="button" aria-expanded={bodyOpen} className="mt-1 text-caption text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
          onClick={() => setBodyOpen(!bodyOpen)}>{bodyOpen ? "Show less" : "Show all"}</button>}
      </>}
    </div>}
    {live && changes !== null && <form className="mt-2 grid gap-2" onSubmit={(event) => { event.preventDefault(); if (changes.trim()) void send("request_changes", changes.trim()); }}>
      <label className="grid gap-1 text-ui">What should change?
        <textarea autoFocus value={changes} onChange={(event) => setChanges(event.target.value)} rows={2} maxLength={4000} className={FIELD} />
      </label>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={pending || !changes.trim()}>Send changes</Button>
        <Button type="button" size="sm" variant="ghost" disabled={pending} onClick={() => setChanges(null)}>Cancel</Button>
      </div>
    </form>}
    {live && changes === null && (buttons.length
      ? <>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {buttons.map((b) => <Button key={b.id} type="button" size="sm" disabled={pending || !!b.disabledReason}
            aria-describedby={b.disabledReason ? reasonId : undefined}
            variant={b.style === "primary" ? "default" : "outline"}
            onClick={() => (b.id === "request_changes" ? setChanges("") : void send(b.id))}>{b.label}</Button>)}
          {compact && onOpen && <button type="button" className="ml-auto text-ui text-muted-foreground underline-offset-2 hover:text-foreground hover:underline" onClick={onOpen}>Open task</button>}
        </div>
        {unavailable && <p id={reasonId} className="mt-1 text-caption text-muted-foreground">{unavailable}</p>}
      </>
      // A free-text ask (no options): the answer is what the person types, posted as optionId "answer".
      : <form className="mt-2 grid gap-2" onSubmit={(event) => { event.preventDefault(); if (text.trim()) void send(FREE_TEXT_OPTION, text.trim()); }}>
        <textarea aria-label="Your answer" value={text} onChange={(event) => setText(event.target.value)} rows={compact ? 2 : 3} maxLength={4000} className={FIELD} />
        <div className="flex items-center gap-2">
          <Button type="submit" size="sm" disabled={pending || !text.trim()}>Send answer</Button>
          {compact && onOpen && <button type="button" className="ml-auto text-ui text-muted-foreground underline-offset-2 hover:text-foreground hover:underline" onClick={onOpen}>Open task</button>}
        </div>
      </form>)}
    {!live && compact && onOpen && <button type="button" className="mt-1 text-ui text-muted-foreground underline-offset-2 hover:text-foreground hover:underline" onClick={onOpen}>Open task</button>}
    {note && <p role="alert" className="mt-1 text-ui text-err">{note}</p>}
  </section>;
}
