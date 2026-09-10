"use client";

import { useEffect, useId, useRef, useState } from "react";
import { AtSign, Send } from "lucide-react";
import type { CreateMessageRequest, MemberRef } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { RoomParticipant } from "./types";
import "./messaging.css";

export interface RoomComposerProps {
  text: string;
  onTextChange: (text: string) => void;
  members: readonly RoomParticipant[];
  selectedMentions: readonly MemberRef[];
  onMentionsChange: (mentions: MemberRef[]) => void;
  defaultAgentId?: string;
  clientMessageId: string;
  sendState: "idle" | "sending" | "persisted" | "uncertain" | "rejected";
  onSubmit: (request: CreateMessageRequest) => void | Promise<void>;
  onRetry?: (request: CreateMessageRequest) => void | Promise<void>;
  onReconcile?: (clientMessageId: string) => void;
  replyToMessageId?: string | null;
  threadRootMessageId?: string | null;
  disabled?: boolean;
  label?: string;
  onTypingChange?: (typing: boolean) => void;
}

const sameMember = (left: MemberRef, right: MemberRef) => left.kind === right.kind && left.id === right.id;

export function RoomComposer(props: RoomComposerProps) {
  return <ComposerAttempt key={props.clientMessageId} {...props} />;
}

function ComposerAttempt({
  text,
  onTextChange,
  members,
  selectedMentions,
  onMentionsChange,
  defaultAgentId,
  clientMessageId,
  sendState,
  onSubmit,
  onRetry,
  onReconcile,
  replyToMessageId = null,
  threadRootMessageId = null,
  disabled = false,
  label = "Message",
  onTypingChange,
}: RoomComposerProps) {
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

  if (previousState !== sendState) {
    setPreviousState(sendState);
    if (sendState !== "idle") {
      setSubmitted(true);
      setLastEvidence(sendState);
    }
    if (sendState === "rejected") setRetryPending(false);
    if (sendState === "rejected" || sendState === "persisted") setCallbackUncertain(false);
  }

  const defaultMention = defaultAgentId ? { kind: "agent" as const, id: defaultAgentId } : null;
  const mentions = [...selectedMentions];
  if (defaultMention && !mentions.some((mention) => sameMember(mention, defaultMention))) mentions.push(defaultMention);
  const authorized = mentions.every((mention) => members.some((member) => sameMember(member, mention)));
  const bytes = new TextEncoder().encode(text).length;
  const error = bytes > 100_000
    ? "Keep your message within 100,000 UTF-8 bytes."
    : mentions.length > 32
      ? "Choose at most 32 room members."
      : !authorized
        ? "A selected mention is no longer in this room."
        : "";
  const effectiveState = sendState === "idle" ? lastEvidence : sendState;
  const uncertain = effectiveState === "uncertain" || callbackUncertain;
  const pending = effectiveState === "sending" || (effectiveState === "idle" && submitted && !uncertain);
  const canSend = !disabled && !error && text.trim().length > 0 && !!clientMessageId && effectiveState === "idle" && !submitted && !uncertain;
  const editable = !disabled && !pending && !uncertain && effectiveState !== "persisted" && !retryPending;
  const status = uncertain
    ? "Message status is uncertain. Check before sending again."
    : pending || retryPending
      ? "Sending message…"
      : effectiveState === "rejected"
        ? "Message rejected. Your draft is here; retry when ready."
        : effectiveState === "persisted"
          ? "Message saved"
          : "";

  function request(): CreateMessageRequest {
    return { clientMessageId, text, mentions, replyToMessageId, threadRootMessageId };
  }

  function submit(retry = false) {
    if (retry
      ? disabled || !!error || !text.trim() || !clientMessageId || effectiveState !== "rejected" || !onRetry || retryLocked.current
      : !canSend || locked.current) return;
    locked.current = true;
    if (retry) {
      retryLocked.current = true;
      setRetryPending(true);
    }
    setSubmitted(true);
    onTypingChange?.(false);
    const version = evidenceVersion.current;
    const markUncertain = () => {
      if (version !== evidenceVersion.current) return;
      setCallbackUncertain(true);
      setRetryPending(false);
    };
    try {
      void Promise.resolve((retry ? onRetry! : onSubmit)(request())).catch(markUncertain);
    } catch {
      markUncertain();
    }
  }

  function toggleMention(member: RoomParticipant, checked: boolean) {
    const ref: MemberRef = { kind: member.kind, id: member.id };
    onMentionsChange(checked
      ? [...selectedMentions.filter((mention) => !sameMember(mention, ref)), ref]
      : selectedMentions.filter((mention) => !sameMember(mention, ref)));
    if (!checked || member.kind !== "agent" || !member.mentionHandle) return;
    const token = `@${member.mentionHandle}`;
    const exists = new RegExp(`(^|\\s)${token}(?=$|\\s|[.,!?;:])`, "i").test(text);
    if (!exists) onTextChange(`${text}${text && !/\s$/.test(text) ? " " : ""}${token} `);
  }

  const visibleMembers = members.filter((member) => {
    const needle = query.toLocaleLowerCase();
    return member.displayName.toLocaleLowerCase().includes(needle) || member.mentionHandle?.includes(needle);
  });

  return <form className="room-composer" aria-label={`${label} composer`} aria-busy={pending || retryPending}
    onSubmit={(event) => { event.preventDefault(); submit(); }}>
    <label className="font-semibold text-sm" htmlFor={fieldId}>{label}</label>
    <Textarea id={fieldId} value={text} disabled={!editable} rows={3} aria-invalid={!!error}
      aria-describedby={`${fieldId}-help ${fieldId}-status`} placeholder="Write a message…"
      onChange={(event) => { onTextChange(event.target.value); onTypingChange?.(!!event.target.value.trim()); }}
      onBlur={() => onTypingChange?.(false)} onCompositionStart={() => { composing.current = true; }}
      onCompositionEnd={() => { composing.current = false; }}
      onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) {
          event.preventDefault();
          submit();
        }
      }} />
    {mentions.length > 0 && <p className="text-sm text-muted-foreground room-wrap">Mentioning {mentions.map((mention) => {
      const member = members.find((candidate) => sameMember(candidate, mention));
      return member ? member.mentionHandle ? `@${member.mentionHandle}` : member.displayName : "Unavailable member";
    }).join(", ")}</p>}
    <div className="room-composer-actions">
      <Button type="button" variant="ghost" aria-label="Choose mentions" aria-expanded={pickerOpen}
        aria-controls={`${fieldId}-targets`} disabled={!editable} onClick={() => setPickerOpen(!pickerOpen)}><AtSign aria-hidden="true" />Mention</Button>
      <Button type="submit" disabled={!canSend} aria-label="Send message"><Send aria-hidden="true" />Send</Button>
    </div>
    {pickerOpen && <fieldset id={`${fieldId}-targets`} className="room-picker" disabled={!editable}>
      <legend className="font-semibold">Room members</legend>
      <label htmlFor={`${fieldId}-search`} className="sr-only">Find room members</label>
      <input id={`${fieldId}-search`} type="search" className="room-input" value={query} onChange={(event) => setQuery(event.target.value)} />
      <div className="room-picker-list">
        {visibleMembers.map((member) => {
          const checked = mentions.some((mention) => sameMember(mention, member));
          return <label key={`${member.kind}:${member.id}`} className="room-choice">
            <input type="checkbox" checked={checked} disabled={member.id === defaultAgentId || (!checked && mentions.length >= 32)}
              onChange={(event) => toggleMention(member, event.target.checked)} />
            <span className="min-w-0"><span className="block truncate" title={member.displayName}>{member.displayName}</span>
              <span className="text-muted-foreground text-sm">{member.kind === "agent" && member.mentionHandle ? `@${member.mentionHandle}` : "Person"}</span></span>
          </label>;
        })}
        {visibleMembers.length === 0 && <p className="text-sm text-muted-foreground">No matching room members.</p>}
      </div>
    </fieldset>}
    <p id={`${fieldId}-help`} className={error ? "text-sm text-err" : "text-sm text-muted-foreground"}>{error || "Enter to send · Shift+Enter for a new line."}</p>
    <p id={`${fieldId}-status`} role="status" className={uncertain ? "text-sm text-warn" : effectiveState === "rejected" ? "text-sm text-err" : "text-sm text-muted-foreground"}>{status}</p>
    {uncertain && onReconcile && <Button type="button" variant="outline" disabled={disabled} onClick={() => onReconcile(clientMessageId)}>Check message status</Button>}
    {!uncertain && effectiveState === "rejected" && onRetry && <Button type="button" variant="outline" disabled={disabled || !!error || !text.trim() || retryPending} onClick={() => submit(true)}>Retry message</Button>}
  </form>;
}
