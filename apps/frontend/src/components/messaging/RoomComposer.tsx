"use client";

import { useEffect, useId, useRef, useState } from "react";
import { AtSign, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { RoomParticipant } from "./types";
import "./messaging.css";

export interface RoomSubmission {
  body: string;
  targetRefs: string[];
  clientRequestId: string;
}

export interface RoomComposerProps {
  body: string;
  onBodyChange: (body: string) => void;
  targets: readonly RoomParticipant[];
  selectedTargetRefs: readonly string[];
  onTargetsChange: (refs: string[]) => void;
  /** Only human–agent DM metadata may supply this default. */
  defaultAgentRef?: string;
  /** Stable for the entire attempt, including rejection or uncertain persistence. */
  clientRequestId: string;
  sendState: "idle" | "sending" | "persisted" | "uncertain" | "rejected";
  onSubmit: (submission: RoomSubmission) => void | Promise<void>;
  onRetry?: (submission: RoomSubmission) => void | Promise<void>;
  onReconcile?: (clientRequestId: string) => void;
  disabled?: boolean;
  label?: string;
  /** Typing adapters must transmit activity only, never the draft body. */
  onTypingChange?: (typing: boolean) => void;
}

/** The caller owns drafts and persistence evidence. A resolved callback is not a receipt. */
export function RoomComposer(props: RoomComposerProps) {
  return <ComposerAttempt key={props.clientRequestId} {...props} />;
}

function ComposerAttempt({ body, onBodyChange, targets, selectedTargetRefs, onTargetsChange,
  defaultAgentRef, clientRequestId, sendState, onSubmit, onRetry, onReconcile,
  disabled = false, label = "Message", onTypingChange }: RoomComposerProps) {
  const fieldId = useId();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState(sendState !== "idle");
  const [callbackUncertain, setCallbackUncertain] = useState(false);
  const [retryPending, setRetryPending] = useState(false);
  const [previousState, setPreviousState] = useState(sendState);
  const [lastEvidence, setLastEvidence] = useState(sendState);
  const composing = useRef(false);
  const locked = useRef(sendState !== "idle");
  const retryLocked = useRef(false);
  const evidenceVersion = useRef(0);
  useEffect(() => {
    if (sendState === "rejected") retryLocked.current = false;
    if (sendState === "rejected" || sendState === "persisted") evidenceVersion.current += 1;
  }, [sendState]);

  // Never unlock an attempt on idle. Only a new supplied identity starts a new send.
  if (previousState !== sendState) {
    setPreviousState(sendState);
    if (sendState !== "idle") { setSubmitted(true); setLastEvidence(sendState); }
    if (sendState === "rejected") setRetryPending(false);
    if (sendState === "rejected" || sendState === "persisted") setCallbackUncertain(false);
  }
  const effectiveState = sendState === "idle" ? lastEvidence : sendState;
  const refs = [...new Set([...selectedTargetRefs, ...(defaultAgentRef ? [defaultAgentRef] : [])])];
  const invalidTarget = refs.some(ref => !targets.some(target => target.ref === ref)) ||
    (defaultAgentRef !== undefined && !targets.some(target => target.ref === defaultAgentRef && target.kind === "agent"));
  const bytes = new TextEncoder().encode(body).length;
  const error = bytes > 16384 ? "Keep your message within 16 KiB of text." : refs.length > 10
    ? "Choose at most 10 people and agents." : invalidTarget ? "A selected recipient is no longer available. Update your selection." : "";
  const uncertain = effectiveState === "uncertain" || callbackUncertain;
  const pending = effectiveState === "sending" || (effectiveState === "idle" && submitted && !uncertain);
  const canSend = !disabled && !error && body.trim().length > 0 && !!clientRequestId && effectiveState === "idle" && !submitted && !uncertain;
  const editable = !disabled && !pending && !uncertain && effectiveState !== "persisted" && !retryPending;
  const status = uncertain ? "Message status is uncertain. Check before sending again."
    : pending || retryPending ? "Sending message…"
    : effectiveState === "rejected" ? "Message rejected. Your draft is here; retry when ready."
    : effectiveState === "persisted" ? "Message saved"
    : "";

  function submit(retry = false) {
    if (retry ? disabled || !!error || !body.trim() || !clientRequestId || effectiveState !== "rejected" || !onRetry || retryLocked.current : !canSend || locked.current) return;
    locked.current = true;
    if (retry) { retryLocked.current = true; setRetryPending(true); }
    setSubmitted(true);
    onTypingChange?.(false);
    const submission = { body, targetRefs: refs, clientRequestId };
    const version = evidenceVersion.current;
    const markUncertain = () => {
      if (version !== evidenceVersion.current) return;
      setCallbackUncertain(true);
      setRetryPending(false);
    };
    // Catch failures without logging private text or inventing persistence evidence.
    try {
      void Promise.resolve((retry ? onRetry! : onSubmit)(submission)).catch(markUncertain);
    } catch {
      markUncertain();
    }
  }

  return <form className="room-composer" aria-label={`${label} composer`} aria-busy={pending || retryPending}
    onSubmit={event => { event.preventDefault(); submit(); }}>
    <label className="font-semibold text-sm" htmlFor={fieldId}>{label}</label>
    <Textarea id={fieldId} value={body} disabled={!editable} rows={3} aria-invalid={!!error}
      aria-describedby={`${fieldId}-help ${fieldId}-status`} placeholder="Write a message…"
      onChange={event => { onBodyChange(event.target.value); onTypingChange?.(!!event.target.value.trim()); }}
      onBlur={() => onTypingChange?.(false)} onCompositionStart={() => { composing.current = true; }}
      onCompositionEnd={() => { composing.current = false; }}
      onKeyDown={event => {
        if (event.key === "Enter" && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) {
          event.preventDefault(); submit();
        }
      }} />
    {refs.length > 0 && <p className="text-sm text-muted-foreground room-wrap">{refs.map(ref => {
      const target = targets.find(candidate => candidate.ref === ref);
      return target ? `${target.kind === "agent" ? "Request work from" : "Notify"} ${target.name}` : "Unavailable recipient";
    }).join(" · ")}</p>}
    <div className="room-composer-actions">
      <Button type="button" variant="ghost" aria-label="Choose mentions and agents" aria-expanded={pickerOpen}
        aria-controls={`${fieldId}-targets`} disabled={!editable} onClick={() => setPickerOpen(!pickerOpen)}><AtSign aria-hidden="true" />Mention</Button>
      <Button type="submit" disabled={!canSend} aria-label="Send message"><Send aria-hidden="true" />Send</Button>
    </div>
    {pickerOpen && <fieldset id={`${fieldId}-targets`} className="room-picker" disabled={!editable}>
      <legend className="font-semibold">People and agents</legend>
      <label htmlFor={`${fieldId}-search`} className="sr-only">Find message recipients</label>
      <input id={`${fieldId}-search`} type="search" className="room-input" value={query} onChange={event => setQuery(event.target.value)} />
      <div className="room-picker-list">
        {targets.filter(target => target.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())).map(target => <label key={target.ref} className="room-choice">
          <input type="checkbox" checked={refs.includes(target.ref)} disabled={target.ref === defaultAgentRef || (!refs.includes(target.ref) && refs.length >= 10)}
            onChange={event => onTargetsChange(event.target.checked ? [...new Set([...selectedTargetRefs, target.ref])] : selectedTargetRefs.filter(ref => ref !== target.ref))} />
          <span className="min-w-0"><span className="block truncate" title={target.name}>{target.name}</span><span className="text-muted-foreground text-sm">{target.kind === "agent" ? "Agent · Request work" : "Person · Notify"}</span></span>
        </label>)}
        {!targets.some(target => target.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())) && <p className="text-sm text-muted-foreground">No matching recipients.</p>}
      </div>
    </fieldset>}
    <p id={`${fieldId}-help`} className={error ? "text-sm text-err" : "text-sm text-muted-foreground"}>{error || "Enter to send · Shift+Enter for a new line. Mentions notify people; selected agents receive work."}</p>
    {selectedTargetRefs.some(ref => !targets.some(target => target.ref === ref)) && <Button type="button" variant="outline" disabled={!editable}
      onClick={() => onTargetsChange(selectedTargetRefs.filter(ref => targets.some(target => target.ref === ref)))}>Remove unavailable recipients</Button>}
    <p id={`${fieldId}-status`} role="status" className={uncertain ? "text-sm text-warn" : effectiveState === "rejected" ? "text-sm text-err" : "text-sm text-muted-foreground"}>{status}</p>
    {uncertain && onReconcile && <Button type="button" variant="outline" disabled={disabled} onClick={() => onReconcile(clientRequestId)}>Check message status</Button>}
    {!uncertain && effectiveState === "rejected" && onRetry && <Button type="button" variant="outline" disabled={disabled || !!error || !body.trim() || retryPending} onClick={() => submit(true)}>Retry message</Button>}
  </form>;
}
