"use client";

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { AtSign, Bold, CaseSensitive, Code, Italic, ImagePlus, Link2, List, ListOrdered, Send, SquareCode, Strikethrough, X } from "lucide-react";
import { activeTrigger, applyCompletion, resolveTypedMentions, respondHint, searchEmoji, wrapSelection, type CompletionTrigger, type CreateMessageRequest, type MarkdownStyle, type MessageAttachment, type MemberRef, type RoomKind } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { consumeFocusComposerRequest } from "@/lib/focusComposerRequest";
import { EmojiPicker, recordRecentEmoji } from "./EmojiPicker";
import { ParticipantAvatar } from "./ParticipantAvatar";
import type { RoomParticipant } from "./types";
import "./messaging.css";

export interface RoomComposerProps {
  text: string;
  onTextChange: (text: string) => void;
  members: readonly RoomParticipant[];
  focusAgentId?: string;
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
  onUploadAttachment?: (image: File) => Promise<MessageAttachment>;
  onEditLast?: () => void;
  dropTargetRef?: RefObject<HTMLElement | null>;
  roomKind?: RoomKind;
  runningAgentIds?: readonly string[];
  listeningAvailable?: boolean;
}

const sameMember = (left: MemberRef, right: MemberRef) => left.kind === right.kind && left.id === right.id;

const FORMAT_CONTROLS: { style: MarkdownStyle; label: string; Icon: typeof Bold; keys?: string }[] = [
  { style: "bold", label: "Bold", Icon: Bold, keys: "Meta+B Control+B" },
  { style: "italic", label: "Italic", Icon: Italic, keys: "Meta+I Control+I" },
  { style: "strike", label: "Strikethrough", Icon: Strikethrough, keys: "Meta+Shift+X Control+Shift+X" },
  { style: "ul", label: "Bulleted list", Icon: List },
  { style: "ol", label: "Numbered list", Icon: ListOrdered },
  { style: "code", label: "Inline code", Icon: Code },
  { style: "codeblock", label: "Code block", Icon: SquareCode, keys: "Meta+Shift+C Control+Shift+C" },
  { style: "link", label: "Link", Icon: Link2 },
];

// The mention list and the emoji list are identical chrome around a
// different row body — one listbox, ARIA wiring, and pick/hover behaviour.
function TypeaheadList<T>({ id, label, items, activeIndex, optionId, renderRow, onHover, onPick }: {
  id: string;
  label: string;
  items: readonly T[];
  activeIndex: number;
  optionId: (index: number) => string;
  renderRow: (item: T) => ReactNode;
  onHover: (index: number) => void;
  onPick: (index: number) => void;
}) {
  return <div id={id} className="room-typeahead" role="listbox" aria-label={label}>
    {items.map((item, index) => <button key={optionId(index)} id={optionId(index)} type="button" role="option" tabIndex={-1}
      aria-selected={index === activeIndex} className="room-choice room-choice-button" data-active={index === activeIndex || undefined}
      onMouseDown={(event) => event.preventDefault()} onMouseEnter={() => onHover(index)} onClick={() => onPick(index)}>
      {renderRow(item)}
    </button>)}
  </div>;
}

export function RoomComposer(props: RoomComposerProps) {
  const restoreFocusAfterSend = useRef(false);
  const requestFocusAfterSend = useCallback(() => { restoreFocusAfterSend.current = true; }, []);
  const cancelFocusAfterSend = useCallback(() => { restoreFocusAfterSend.current = false; }, []);
  const consumeFocusAfterSend = useCallback(() => {
    if (!restoreFocusAfterSend.current) return false;
    restoreFocusAfterSend.current = false;
    return true;
  }, []);
  return <ComposerAttempt key={props.clientMessageId} {...props} requestFocusAfterSend={requestFocusAfterSend}
    cancelFocusAfterSend={cancelFocusAfterSend} consumeFocusAfterSend={consumeFocusAfterSend} />;
}

function ComposerAttempt({
  text,
  onTextChange,
  members,
  focusAgentId,
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
  onUploadAttachment,
  onEditLast,
  dropTargetRef,
  roomKind,
  runningAgentIds = [],
  listeningAvailable = true,
  requestFocusAfterSend,
  cancelFocusAfterSend,
  consumeFocusAfterSend,
}: RoomComposerProps & {
  requestFocusAfterSend: () => void;
  cancelFocusAfterSend: () => void;
  consumeFocusAfterSend: () => boolean;
}) {
  const fieldId = useId();
  const field = useRef<HTMLTextAreaElement>(null);
  const [trigger, setTrigger] = useState<CompletionTrigger | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [submitted, setSubmitted] = useState(sendState !== "idle");
  const [callbackUncertain, setCallbackUncertain] = useState(false);
  const [retryPending, setRetryPending] = useState(false);
  const [previousState, setPreviousState] = useState(sendState);
  const [lastEvidence, setLastEvidence] = useState(sendState);
  const composing = useRef(false);
  const locked = useRef(sendState !== "idle");
  const retryLocked = useRef(false);
  const imageInput = useRef<HTMLInputElement>(null);
  const previews = useRef(new Set<string>());
  const [images, setImages] = useState<Array<{ file: File; preview: string; attachment?: MessageAttachment; error?: string }>>([]);
  const evidenceVersion = useRef(0);
  const [dropping, setDropping] = useState(false);
  const dragDepthRef = useRef(0);
  const externalDragDepthRef = useRef(0);
  const addImagesRef = useRef<(files: File[]) => void>(() => {});
  const [toolbarIndex, setToolbarIndex] = useState(0);
  // The formatting bar shows by default (compact, at the top of the box) and
  // the Aa button collapses it; the keyboard shortcuts work either way.
  const [formatOpen, setFormatOpen] = useState(true);
  // A successful send rotates clientMessageId and deliberately remounts this
  // attempt. Preserve keyboard focus across that reset when Enter initiated it.
  useLayoutEffect(() => {
    if (consumeFocusAfterSend()) field.current?.focus();
  }, [consumeFocusAfterSend]);
  // One line that grows with the draft, instead of a fixed three-row box.
  useLayoutEffect(() => {
    const node = field.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 200)}px`;
  }, [text]);
  const toolbarButtons = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => () => previews.current.forEach((preview) => URL.revokeObjectURL(preview)), []);

  useEffect(() => {
    if (!focusAgentId) return;
    const applyFocusRequest = (event?: Event) => {
      const requestedAgent = (event as CustomEvent<{ agentId?: string }> | undefined)?.detail?.agentId;
      if (requestedAgent && requestedAgent !== focusAgentId) return;
      const request = consumeFocusComposerRequest(focusAgentId);
      if (!request) return;
      if (request.draft && !text.trim()) onTextChange(request.draft);
      field.current?.focus();
    };
    applyFocusRequest();
    window.addEventListener("getstarted:focus-composer", applyFocusRequest);
    return () => window.removeEventListener("getstarted:focus-composer", applyFocusRequest);
  }, [focusAgentId, onTextChange, text]);

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

  // A DM with exactly one agent puts that agent first — the same signal the
  // context already uses for focusAgentId.
  const mentionable = focusAgentId
    ? [...members.filter((member) => member.id === focusAgentId), ...members.filter((member) => member.id !== focusAgentId)]
    : members;
  const options = trigger?.kind === "mention"
    ? mentionable.filter((member) => {
        const needle = trigger.query.toLocaleLowerCase();
        return !needle
          || member.displayName.toLocaleLowerCase().includes(needle)
          || (member.mentionHandle ?? "").toLocaleLowerCase().includes(needle);
      })
    : [];
  const mentionOpen = options.length > 0;
  const emojiOptions = trigger?.kind === "emoji" ? searchEmoji(trigger.query).slice(0, 8) : [];
  const emojiOpen = emojiOptions.length > 0;
  const listOpen = mentionOpen || emojiOpen;
  // Mentions are derived purely from the typed text — what renders as a
  // structured mention on send is exactly what the composer's own typeahead
  // inserted (or what the sender typed by hand). There is no separate
  // "picked mentions" side-state to drift from the text.
  const mentions = resolveTypedMentions(text, members);
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
  const uploading = images.some((image) => !image.attachment && !image.error);
  const uploadFailed = images.some((image) => image.error);
  const hasContent = text.trim().length > 0 || images.some((image) => image.attachment);
  const canSend = !disabled && !error && !uploading && !uploadFailed && hasContent && !!clientMessageId && effectiveState === "idle" && !submitted && !uncertain;
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
    return { clientMessageId, text, mentions, replyToMessageId, threadRootMessageId, attachmentIds: images.flatMap((image) => image.attachment ? [image.attachment.id] : []) };
  }

  function addImages(files: File[]) {
    if (!onUploadAttachment || !editable) return;
    const room = Math.max(0, 4 - images.length);
    for (const file of files.slice(0, room)) {
      const preview = URL.createObjectURL(file);
      previews.current.add(preview);
      setImages((current) => [...current, { file, preview }]);
      void onUploadAttachment(file).then(
        (attachment) => setImages((current) => current.map((image) => image.preview === preview ? { ...image, attachment } : image)),
        (reason) => setImages((current) => current.map((image) => image.preview === preview ? { ...image, error: reason instanceof Error ? reason.message : "Upload failed" } : image)),
      );
    }
  }

  function removeImage(preview: string) {
    if (!editable) return;
    URL.revokeObjectURL(preview);
    previews.current.delete(preview);
    setImages((current) => current.filter((image) => image.preview !== preview));
  }

  // Refs can't be written during render (React may call render without
  // committing) — keep the ref current after every commit instead, so the
  // dropTargetRef listener effect below (bound once) always calls the latest
  // addImages via the ref rather than a stale closure.
  useEffect(() => {
    addImagesRef.current = addImages;
  });

  const carriesFiles = (transfer: DataTransfer | null) => !!transfer && [...transfer.types].includes("Files");

  const handleFormDragEnter = (event: React.DragEvent<HTMLFormElement>) => {
    if (!carriesFiles(event.dataTransfer)) return;
    dragDepthRef.current += 1;
    setDropping(true);
  };

  const handleFormDragLeave = (event: React.DragEvent<HTMLFormElement>) => {
    if (!carriesFiles(event.dataTransfer)) return;
    dragDepthRef.current -= 1;
    if (dragDepthRef.current === 0) setDropping(false);
  };

  const handleFormDrop = (event: React.DragEvent<HTMLFormElement>) => {
    if (!carriesFiles(event.dataTransfer)) return;
    event.preventDefault();
    dragDepthRef.current = 0;
    externalDragDepthRef.current = 0;
    setDropping(false);
    const files = [...(event.dataTransfer?.files ?? [])].filter((file) => file.type.startsWith("image/"));
    if (files.length) addImages(files);
  };

  useEffect(() => {
    const node = dropTargetRef?.current;
    if (!node) return;

    const enter = (event: DragEvent) => {
      if (!carriesFiles(event.dataTransfer)) return;
      externalDragDepthRef.current += 1;
      setDropping(true);
    };
    const leave = (event: DragEvent) => {
      if (!carriesFiles(event.dataTransfer)) return;
      externalDragDepthRef.current -= 1;
      if (externalDragDepthRef.current === 0) setDropping(false);
    };
    const drop = (event: DragEvent) => {
      if (!carriesFiles(event.dataTransfer)) return;
      event.preventDefault();
      dragDepthRef.current = 0;
      externalDragDepthRef.current = 0;
      setDropping(false);
      const files = [...(event.dataTransfer?.files ?? [])].filter((file) => file.type.startsWith("image/"));
      if (files.length) addImagesRef.current(files);
    };
    const windowDragEnd = () => {
      dragDepthRef.current = 0;
      externalDragDepthRef.current = 0;
      setDropping(false);
    };
    const windowDrop = () => {
      dragDepthRef.current = 0;
      externalDragDepthRef.current = 0;
      setDropping(false);
    };

    node.addEventListener("dragenter", enter);
    node.addEventListener("dragleave", leave);
    node.addEventListener("drop", drop);
    window.addEventListener("dragend", windowDragEnd);
    window.addEventListener("drop", windowDrop);

    return () => {
      node.removeEventListener("dragenter", enter);
      node.removeEventListener("dragleave", leave);
      node.removeEventListener("drop", drop);
      window.removeEventListener("dragend", windowDragEnd);
      window.removeEventListener("drop", windowDrop);
    };
    // addImages is read through a ref so the listeners bind once and never see stale state
  }, [dropTargetRef]);

  function syncTrigger(value: string, caret: number | null) {
    if (composing.current) return;
    setTrigger(activeTrigger(value, caret));
    setActiveIndex(0);
  }

  function focusRange(start: number, end: number) {
    requestAnimationFrame(() => {
      field.current?.focus();
      field.current?.setSelectionRange(start, end);
    });
  }

  // The one place that both commits a text rewrite and clears a completion
  // trigger, so a caller can never rewrite the text and forget the trigger is
  // now pointing at stale byte offsets.
  function replaceText(next: string, start: number, end: number) {
    setTrigger(null);
    onTextChange(next);
    focusRange(start, end);
  }

  function choose(member: RoomParticipant) {
    if (!trigger) return;
    const token = member.mentionHandle ? `@${member.mentionHandle}` : `@${member.displayName}`;
    const next = applyCompletion(text, trigger, token);
    replaceText(next.text, next.caret, next.caret);
  }

  function insertAtCaret(value: string) {
    const start = field.current?.selectionStart ?? text.length;
    const end = field.current?.selectionEnd ?? start;
    const next = `${text.slice(0, start)}${value}${text.slice(end)}`;
    replaceText(next, start + value.length, start + value.length);
  }

  function chooseEmoji(emoji: string) {
    if (!trigger) return;
    const next = applyCompletion(text, trigger, emoji);
    recordRecentEmoji(emoji);
    replaceText(next.text, next.caret, next.caret);
  }

  function submit(retry = false, restoreFocus = false) {
    if (retry
      ? disabled || !!error || uploading || uploadFailed || !hasContent || !clientMessageId || effectiveState !== "rejected" || !onRetry || retryLocked.current
      : !canSend || locked.current) return;
    locked.current = true;
    if (restoreFocus) requestFocusAfterSend();
    if (retry) {
      retryLocked.current = true;
      setRetryPending(true);
    }
    setSubmitted(true);
    onTypingChange?.(false);
    const version = evidenceVersion.current;
    const markUncertain = () => {
      cancelFocusAfterSend();
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

  function format(style: MarkdownStyle) {
    const start = field.current?.selectionStart ?? text.length;
    const end = field.current?.selectionEnd ?? start;
    const next = wrapSelection(text, start, end, style);
    replaceText(next.text, next.selectionStart, next.selectionEnd);
  }

  const hint = respondHint(roomKind, members, mentions, runningAgentIds, listeningAvailable);
  // Who-will-reply matters at the moment of sending, not while the box is empty.
  const showHint = !!hint && hasContent;

  return <form className="room-composer" aria-label={`${label} composer`} aria-busy={pending || retryPending}
    data-dropping={dropping || undefined}
    onSubmit={(event) => { event.preventDefault(); submit(); }}
    onDragEnter={handleFormDragEnter}
    onDragOver={(event) => { if (carriesFiles(event.dataTransfer)) event.preventDefault(); }}
    onDragLeave={handleFormDragLeave}
    onDrop={handleFormDrop}>
    {formatOpen && <div id={`${fieldId}-format`} className="room-format" role="toolbar" aria-label="Formatting"
      onKeyDown={(event) => {
        const count = FORMAT_CONTROLS.length;
        let next: number;
        if (event.key === "ArrowRight") next = (toolbarIndex + 1) % count;
        else if (event.key === "ArrowLeft") next = (toolbarIndex - 1 + count) % count;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = count - 1;
        else return;
        event.preventDefault();
        setToolbarIndex(next);
        toolbarButtons.current[next]?.focus();
      }}>
      {FORMAT_CONTROLS.map(({ style, label: controlLabel, Icon, keys }, index) => <Button key={style} type="button" variant="ghost" size="icon-sm"
        ref={(node) => { toolbarButtons.current[index] = node; }} tabIndex={index === toolbarIndex ? 0 : -1}
        aria-label={controlLabel} aria-keyshortcuts={keys} title={controlLabel} disabled={!editable}
        onFocus={() => setToolbarIndex(index)} onClick={() => format(style)}><Icon aria-hidden="true" /></Button>)}
    </div>}
    {dropping && <p className="room-drop-hint text-sm text-muted-foreground" role="status">Drop images to attach</p>}
    <label className="sr-only" htmlFor={fieldId}>{label}</label>
    {mentionOpen && <TypeaheadList id={`${fieldId}-targets`} label="Mention a room member" items={options} activeIndex={activeIndex}
      optionId={(index) => `${fieldId}-option-${index}`} onHover={setActiveIndex} onPick={(index) => choose(options[index])}
      renderRow={(member) => <>
        <ParticipantAvatar participant={member} size="sm" />
        <span className="min-w-0 flex-1 text-left"><span className="block truncate" title={member.displayName}>{member.displayName}</span>
          <span className="text-muted-foreground text-sm">{member.mentionHandle ? `@${member.mentionHandle}` : "Person"}</span></span>
      </>} />}
    {emojiOpen && <TypeaheadList id={`${fieldId}-targets`} label="Insert an emoji" items={emojiOptions} activeIndex={activeIndex}
      optionId={(index) => `${fieldId}-option-${index}`} onHover={setActiveIndex} onPick={(index) => chooseEmoji(emojiOptions[index].emoji)}
      renderRow={(entry) => <><span aria-hidden="true" className="text-lg">{entry.emoji}</span><span className="flex-1 text-left truncate">{entry.name}</span></>} />}
    <Textarea ref={field} id={fieldId} value={text} disabled={!editable} rows={1} aria-invalid={!!error}
      aria-describedby={`${fieldId}-help ${fieldId}-status${showHint ? ` ${fieldId}-respond` : ""}`} placeholder={label === "Reply to thread" ? "Reply to thread…" : "Message the conversation · @ to mention"}
      aria-autocomplete="list" aria-expanded={listOpen} aria-controls={listOpen ? `${fieldId}-targets` : undefined}
      aria-activedescendant={listOpen ? `${fieldId}-option-${activeIndex}` : undefined}
      onChange={(event) => { onTextChange(event.target.value); onTypingChange?.(!!event.target.value.trim()); syncTrigger(event.target.value, event.target.selectionStart); }}
      onClick={(event) => syncTrigger(event.currentTarget.value, event.currentTarget.selectionStart)}
      onKeyUp={(event) => { if (event.key.startsWith("Arrow") || event.key === "Home" || event.key === "End") syncTrigger(event.currentTarget.value, event.currentTarget.selectionStart); }}
      onPaste={(event) => {
        const files = [...event.clipboardData.files].filter((file) => file.type.startsWith("image/"));
        if (files.length) { event.preventDefault(); addImages(files); }
      }}
      onBlur={() => onTypingChange?.(false)} onCompositionStart={() => { composing.current = true; }}
      onCompositionEnd={(event) => { composing.current = false; syncTrigger(event.currentTarget.value, event.currentTarget.selectionStart); }}
      onKeyDown={(event) => {
        if (listOpen) {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            if (event.nativeEvent.isComposing || event.keyCode === 229) return;
            event.preventDefault();
            const count = mentionOpen ? options.length : emojiOptions.length;
            setActiveIndex((index) => (index + (event.key === "ArrowDown" ? 1 : count - 1)) % count);
            return;
          }
          if (event.key === "Enter" || event.key === "Tab") {
            if (event.nativeEvent.isComposing || event.keyCode === 229) return;
            event.preventDefault();
            if (mentionOpen) choose(options[activeIndex] ?? options[0]);
            else chooseEmoji((emojiOptions[activeIndex] ?? emojiOptions[0]).emoji);
            return;
          }
          if (event.key === "Escape") {
            // Esc closes the typeahead FIRST; the thread/overlay listener on
            // window must not also fire. stopPropagation on the synthetic
            // event is not enough — React listens at the root, below window.
            event.preventDefault();
            event.nativeEvent.stopImmediatePropagation();
            setTrigger(null);
            return;
          }
        }
        // Plain ArrowUp only — ⌥↑ is the window-level previous-room shortcut,
        // and swallowing it here would make it dead from the composer.
        if (event.key === "ArrowUp" && !event.altKey && !text && onEditLast) {
          event.preventDefault();
          onEditLast();
          return;
        }
        if (!listOpen && (event.metaKey || event.ctrlKey)) {
          const key = event.key.toLowerCase();
          const style: MarkdownStyle | null = event.shiftKey
            ? key === "x" ? "strike" : key === "c" ? "codeblock" : null
            : key === "b" ? "bold" : key === "i" ? "italic" : null;
          if (style) { event.preventDefault(); format(style); return; }
        }
        if (event.key === "Enter" && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) {
          event.preventDefault();
          submit(false, true);
        }
      }} />
    {mentions.length > 0 && <p className="text-sm text-muted-foreground room-wrap">Mentioning {mentions.map((mention) => {
      const member = members.find((candidate) => sameMember(candidate, mention));
      return member ? member.mentionHandle ? `@${member.mentionHandle}` : member.displayName : "Unavailable member";
    }).join(", ")}</p>}
    {images.length > 0 && <div className="room-image-previews">{images.map((image) => <div key={image.preview} className="room-image-preview">
      {/* eslint-disable-next-line @next/next/no-img-element */}<img src={image.preview} alt={image.file.name} />
      <Button type="button" variant="ghost" aria-label={`Remove ${image.file.name}`} disabled={!editable} onClick={() => removeImage(image.preview)}><X aria-hidden="true" /></Button>
      <span className={image.error ? "text-err" : "text-muted-foreground"}>{image.error ?? (image.attachment ? "Ready" : "Uploading…")}</span>
    </div>)}</div>}
    <div className="room-composer-actions">
      {onUploadAttachment && <><input ref={imageInput} className="sr-only" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple
        onChange={(event) => { addImages([...event.target.files ?? []]); event.target.value = ""; }} />
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Add images" disabled={!editable || images.length >= 4} onClick={() => imageInput.current?.click()}><ImagePlus aria-hidden="true" /></Button></>}
      <Button type="button" variant="ghost" size="icon-sm" aria-label={formatOpen ? "Hide formatting" : "Show formatting"} aria-pressed={formatOpen}
        aria-controls={formatOpen ? `${fieldId}-format` : undefined} onClick={() => setFormatOpen(!formatOpen)}><CaseSensitive aria-hidden="true" /></Button>
      <EmojiPicker triggerLabel="Insert emoji" disabled={!editable} onPick={insertAtCaret} />
      <Button type="button" variant="ghost" size="icon-sm" aria-label="Mention someone" aria-expanded={mentionOpen}
        disabled={!editable} onClick={() => {
          const caret = field.current?.selectionStart ?? text.length;
          const needsSpace = caret > 0 && !/\s$/.test(text.slice(0, caret));
          const next = `${text.slice(0, caret)}${needsSpace ? " " : ""}@${text.slice(caret)}`;
          const sigil = caret + (needsSpace ? 1 : 0);
          onTextChange(next);
          setTrigger({ kind: "mention", start: sigil, end: sigil + 1, query: "" });
          setActiveIndex(0);
          requestAnimationFrame(() => { field.current?.focus(); field.current?.setSelectionRange(sigil + 1, sigil + 1); });
        }}><AtSign aria-hidden="true" /><span className="sr-only">Mention</span></Button>
      <Button type="submit" size="icon-sm" className="room-send" disabled={!canSend} aria-label="Send message"><Send aria-hidden="true" /><span className="sr-only">Send</span></Button>
    </div>
    {showHint && <p id={`${fieldId}-respond`} className="room-respond-hint text-sm text-muted-foreground">{hint}</p>}
    <p id={`${fieldId}-help`} className={error ? "text-sm text-err" : "sr-only"}>{error || "Enter to send · Shift+Enter for a new line."}</p>
    <p id={`${fieldId}-status`} role="status" className={uncertain ? "text-sm text-warn" : effectiveState === "rejected" ? "text-sm text-err" : "text-sm text-muted-foreground"}>{status}</p>
    {uncertain && onReconcile && <Button type="button" variant="outline" disabled={disabled} onClick={() => onReconcile(clientMessageId)}>Check message status</Button>}
    {!uncertain && effectiveState === "rejected" && onRetry && <Button type="button" variant="outline" disabled={disabled || !!error || uploading || (!text.trim() && !images.some((image) => image.attachment)) || retryPending} onClick={() => submit(true)}>Retry message</Button>}
  </form>;
}
