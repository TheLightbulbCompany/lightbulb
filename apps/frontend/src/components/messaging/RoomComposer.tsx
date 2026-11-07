"use client";

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ComponentType, type ReactNode, type RefObject } from "react";
import { AtSign, Bold, CaseSensitive, Code, FileText, Italic, Link2, List, ListOrdered, Paperclip, Plus, RotateCcw, Send, SmilePlus, SquareCode, Strikethrough, X } from "lucide-react";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { ApiError, MESSAGE_ATTACHMENT_ACCEPT, MESSAGE_ATTACHMENT_MAX_BYTES, MESSAGE_ATTACHMENT_MAX_COUNT, resolveMessageAttachmentContentType, resolveTypedMentions, resolveTypedSessionMentions, respondHint, searchEmoji, sessionMatches, sessionTitle, type CompletionTrigger, type CreateMessageRequest, type MarkdownStyle, type MessageAttachment, type MessageAttachmentContentType, type MemberRef, type RoomKind, type SessionMentionCandidate, type MentionTarget, sessionAgentName } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import type { ComposerEditorHandle, ComposerEditorProps } from "./ComposerEditor";
import { consumeFocusComposerRequest } from "@/lib/focusComposerRequest";
import { formatBytes } from "@/lib/utils";
import { EmojiPicker, recordRecentEmoji } from "./EmojiPicker";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { SessionToolIcon } from "./SessionToolIcon";
import type { RoomParticipant } from "./types";
import "./messaging.css";

export interface RoomComposerSessionOption {
  candidate: SessionMentionCandidate;
  ownerName: string;
}

export interface RoomComposerProps {
  text: string;
  onTextChange: (text: string) => void;
  members: readonly RoomParticipant[];
  sessionTargets?: readonly RoomComposerSessionOption[];
  focusAgentId?: string;
  clientMessageId: string;
  sendState: "idle" | "sending" | "persisted" | "uncertain" | "rejected";
  onSubmit: (request: CreateMessageRequest) => void | Promise<void>;
  onRetry?: (request: CreateMessageRequest) => void | Promise<void>;
  onReconcile?: (clientMessageId: string) => void;
  replyToMessageId?: string | null;
  threadRootMessageId?: string | null;
  disabled?: boolean;
  /** Each change focuses the field with the cursor at the end (not on first mount). */
  focusToken?: number;
  /** Drafts that count as empty (an unfinished suggested reply), so Send stays off until completed. */
  placeholderDrafts?: readonly string[];
  label?: string;
  /** The visible hint in the empty box; the room's own by default. */
  placeholder?: string;
  /** False hides the "who will reply" line (a reply into a request goes to its specialist, not the room's agents). */
  showRespondHint?: boolean;
  onTypingChange?: (typing: boolean) => void;
  onUploadAttachment?: (file: File) => Promise<MessageAttachment>;
  onEditLast?: () => void;
  dropTargetRef?: RefObject<HTMLElement | null>;
  roomKind?: RoomKind;
  runningAgentIds?: readonly string[];
  /** The viewer: with the super-agent on, whether the formatting bar shows is remembered per person. */
  viewerId?: string;
  /** Workspace people who aren't here: the @ list offers them marked "Not in channel", and sending asks to add them (W18-3). */
  outsiders?: readonly MentionTarget[];
  onAddPeople?: (people: MemberRef[]) => Promise<void>;
  /** "#tea shop" or "this group chat", for "Priya isn't in #tea shop". */
  roomLabel?: string;
}

/** Super-agent: the composer is one line with the formatting bar shown above it by default (founder, 6 Nov 2026); Aa
 *  hides it and the choice is kept per person on this browser. ponytail: localStorage; a server preference if it should follow them across devices. */
const formatKey = (viewerId: string) => `lightbulb.composer.format:${viewerId}`;
function readFormatOpen(viewerId: string): boolean {
  try { return localStorage.getItem(formatKey(viewerId)) !== "0"; } catch { return true; }
}
function writeFormatOpen(viewerId: string, open: boolean): void {
  try { localStorage.setItem(formatKey(viewerId), open ? "1" : "0"); } catch { /* private mode: this session only */ }
}

// The WYSIWYG box (Lexical, ~125 KB gzip) is its own chunk, fetched as soon as this module loads; until it lands the
// composer is a plain box with the same text, so nothing typed is lost.
let LoadedEditor: ComponentType<ComposerEditorProps> | null = null;
export const composerEditorReady: Promise<void> = typeof window === "undefined" ? Promise.resolve()
  : import("./ComposerEditor").then((module) => { LoadedEditor = module.ComposerEditor; });
/** The WYSIWYG box once its chunk has landed (the edit form uses it too). */
export const loadedComposerEditor = () => LoadedEditor;

const sameMember = (left: MemberRef, right: MemberRef) => left.kind === right.kind && left.id === right.id;
const uniqueRefs = (refs: MemberRef[]) => refs.filter((ref, index) => refs.findIndex((other) => sameMember(other, ref)) === index);
const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** The picks whose `@token` is still in the text: as many picks per token as the token still appears. */
function keptPicks(text: string, picks: readonly { token: string; ref: MemberRef }[]): MemberRef[] {
  const used = new Map<string, number>();
  return picks.flatMap((pick) => {
    const key = pick.token.toLowerCase();
    const seen = used.get(key) ?? 0;
    const count = text.match(new RegExp(`(^|[\\s(])@${escapeRegExp(pick.token)}(?![\\w.-])`, "gi"))?.length ?? 0;
    if (count <= seen) return [];
    used.set(key, seen + 1);
    return [pick.ref];
  });
}

/** Markdown with nothing to send: empty, or only an empty bullet, number, quote or code fence. */
export const isBlankMarkdown = (text: string) => text.split("\n").every((line) => /^\s*(?:[-*+]|\d+\.|>|```[\w-]*)?\s*$/.test(line));

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

type DraftAttachment = { key: number; file: File; preview?: string; attachment?: MessageAttachment; error?: string };
const NO_ATTACHMENTS: DraftAttachment[] = [];

export function RoomComposer(props: RoomComposerProps) {
  // A focusToken change focuses the field even when it lands with a remount.
  const seenFocusToken = useRef(props.focusToken);
  // Attachments outlive a remount that was not a send (a new clientMessageId):
  // the attempt mirrors them here and the next attempt starts with them unless the last one sent them.
  const carried = useRef<{ items: DraftAttachment[]; sent: boolean }>({ items: [], sent: false });
  useEffect(() => () => { for (const item of carried.current.items) if (item.preview) URL.revokeObjectURL(item.preview); }, []);
  const restoreFocusAfterSend = useRef(false);
  const requestFocusAfterSend = useCallback(() => { restoreFocusAfterSend.current = true; }, []);
  const cancelFocusAfterSend = useCallback(() => { restoreFocusAfterSend.current = false; }, []);
  const consumeFocusAfterSend = useCallback(() => {
    if (!restoreFocusAfterSend.current) return false;
    restoreFocusAfterSend.current = false;
    return true;
  }, []);
  return <ComposerAttempt key={props.clientMessageId} {...props} requestFocusAfterSend={requestFocusAfterSend}
    cancelFocusAfterSend={cancelFocusAfterSend} consumeFocusAfterSend={consumeFocusAfterSend} seenFocusToken={seenFocusToken} carried={carried} />;
}

function ComposerAttempt({
  text,
  onTextChange,
  members,
  sessionTargets = [],
  focusAgentId,
  clientMessageId,
  sendState,
  onSubmit,
  onRetry,
  onReconcile,
  replyToMessageId = null,
  threadRootMessageId = null,
  disabled = false,
  focusToken,
  placeholderDrafts,
  label = "Message",
  placeholder,
  showRespondHint = true,
  onTypingChange,
  onUploadAttachment,
  onEditLast,
  dropTargetRef,
  roomKind,
  runningAgentIds = [],
  viewerId,
  outsiders = [],
  onAddPeople,
  roomLabel = "this chat",
  requestFocusAfterSend,
  cancelFocusAfterSend,
  consumeFocusAfterSend,
  seenFocusToken,
  carried,
}: RoomComposerProps & {
  requestFocusAfterSend: () => void;
  cancelFocusAfterSend: () => void;
  consumeFocusAfterSend: () => boolean;
  seenFocusToken: RefObject<number | undefined>;
  carried: RefObject<{ items: DraftAttachment[]; sent: boolean }>;
}) {
  const fieldId = useId();
  const field = useRef<ComposerEditorHandle>(null);
  useEffect(() => {
    if (focusToken === undefined || focusToken === seenFocusToken.current) return;
    seenFocusToken.current = focusToken;
    field.current?.focus();
  }, [focusToken]);
  const [trigger, setTrigger] = useState<CompletionTrigger | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [selectedSessions, setSelectedSessions] = useState<Record<string, string>>({});
  const [picked, setPicked] = useState<{ token: string; ref: MemberRef }[]>([]);
  const [editorLoaded, setEditorLoaded] = useState(() => !!LoadedEditor);
  useEffect(() => { if (!editorLoaded) void composerEditorReady.then(() => setEditorLoaded(true)); }, [editorLoaded]);
  const plainFocused = useRef(false);
  const [submitted, setSubmitted] = useState(sendState !== "idle");
  const [callbackUncertain, setCallbackUncertain] = useState(false);
  const [retryPending, setRetryPending] = useState(false);
  const [previousState, setPreviousState] = useState(sendState);
  const [lastEvidence, setLastEvidence] = useState(sendState);
  const locked = useRef(sendState !== "idle");
  const retryLocked = useRef(false);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const previews = useRef(new Set<string>());
  const [allAttachments, setAttachments] = useState<DraftAttachment[]>(() => (carried.current.sent ? [] : carried.current.items));
  const nextAttachmentKey = useRef(Math.max(0, ...allAttachments.map((item) => item.key + 1)));
  // A composer that can't upload (a reply into a request) doesn't send them either: they wait for the room's.
  const attachments = onUploadAttachment ? allAttachments : NO_ATTACHMENTS;
  const [attachmentError, setAttachmentError] = useState("");
  const [staleNotice, setStaleNotice] = useState("");
  const evidenceVersion = useRef(0);
  const [dropping, setDropping] = useState(false);
  const dragDepthRef = useRef(0);
  const externalDragDepthRef = useRef(0);
  const addAttachmentsRef = useRef<(files: File[]) => void>(() => {});
  const [toolbarIndex, setToolbarIndex] = useState(0);
  // The box rests as one line and Aa's choice is remembered per person (signed in). The keyboard shortcuts work either way.
  const compact = !!viewerId;
  const [formatPref, setFormatPref] = useState<{ key: string | null; open: boolean }>(() => ({ key: null, open: true }));
  const prefKey = compact ? viewerId! : null;
  if (formatPref.key !== prefKey) setFormatPref({ key: prefKey, open: prefKey ? readFormatOpen(prefKey) : true });
  const formatOpen = formatPref.open;
  const setFormatOpen = (open: boolean) => {
    setFormatPref({ key: prefKey, open });
    if (prefKey) writeFormatOpen(prefKey, open);
  };
  const [pickerOpen, setPickerOpen] = useState(false);
  // The + menu hands focus to the emoji picker it opens, not back to +.
  const emojiPending = useRef(false);
  // A successful send rotates clientMessageId and deliberately remounts this
  // attempt. Preserve keyboard focus across that reset when Enter initiated it.
  useLayoutEffect(() => {
    if (consumeFocusAfterSend()) field.current?.focus();
  }, [consumeFocusAfterSend]);
  const toolbarButtons = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    carried.current.sent = false;
    for (const item of allAttachments) if (item.preview) previews.current.add(item.preview);
    // An upload still running when the last attempt unmounted reported to it: run it again here.
    for (const item of allAttachments) if (!item.attachment && !item.error) uploadItem(item.key, item.file);
    // Mount only: this attempt adopts what the last one left.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { carried.current.items = allAttachments; }, [allAttachments, carried]);
  // Previews die with the attempt only when it sent them; otherwise the next attempt shows them.
  useEffect(() => () => { if (carried.current.sent) previews.current.forEach((preview) => URL.revokeObjectURL(preview)); }, [carried]);

  useEffect(() => {
    if (!focusAgentId) return;
    const applyFocusRequest = (event?: Event) => {
      const requestedAgent = (event as CustomEvent<{ agentId?: string }> | undefined)?.detail?.agentId;
      if (requestedAgent && requestedAgent !== focusAgentId) return;
      const request = consumeFocusComposerRequest(focusAgentId);
      if (!request) return;
      // Never lost to a draft already there (W12-13): it goes under it, unless it is already in it.
      const current = text.trimEnd();
      const draft = request.draft && !current.includes(request.draft.trim()) ? request.draft : null;
      const next = draft ? (current ? `${current}\n\n${draft}` : draft) : null;
      if (next) onTextChange(next);
      field.current?.focus();
      // A starter ask ("Write a doc: …"): its first "…" is selected once it renders, so typing replaces it.
      const blank = next && draft ? draft.indexOf("…") : -1;
      const before = blank >= 0 ? (current.match(/…/g) ?? []).length : 0;
      if (blank >= 0) requestAnimationFrame(() => field.current?.selectText("…", before));
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
  type MentionOption = { kind: "member"; member: MentionTarget; outside?: boolean } | { kind: "session"; option: RoomComposerSessionOption };
  const matchesQuery = (member: MentionTarget) => {
    const needle = trigger?.query.toLocaleLowerCase() ?? "";
    return !needle
      || member.displayName.toLocaleLowerCase().includes(needle)
      || (member.kind === "agent" && (member.mentionHandle ?? "").toLocaleLowerCase().includes(needle));
  };
  // People here first; then, once something is typed, workspace people who aren't (Slack's "Not in channel").
  const canInvite = !!onAddPeople && outsiders.length > 0;
  const memberOptions: MentionOption[] = trigger?.kind === "mention"
    ? [...mentionable.filter(matchesQuery).map((member) => ({ kind: "member" as const, member })),
      ...(canInvite && trigger.query ? outsiders.filter(matchesQuery).slice(0, 5).map((member) => ({ kind: "member" as const, member, outside: true })) : [])]
    : [];
  const sessionOptions: MentionOption[] = trigger?.kind === "mention"
    ? sessionTargets.filter(({ candidate, ownerName }) => {
        const needle = trigger.query.toLocaleLowerCase();
        return candidate.handle.toLocaleLowerCase().includes(needle)
          || sessionAgentName(candidate.session.provider).toLowerCase().includes(needle)
          || sessionMatches(candidate.session, needle, ownerName);
      }).map((option) => ({ kind: "session" as const, option }))
    : [];
  const options = [...memberOptions, ...sessionOptions];
  const mentionOpen = options.length > 0;
  const emojiOptions = trigger?.kind === "emoji" ? searchEmoji(trigger.query).slice(0, 8) : [];
  const emojiOpen = emojiOptions.length > 0;
  const listOpen = mentionOpen || emojiOpen;
  // Mentions come from the text, except where the sender picked someone from the list: that pick is kept by id while
  // its token is still in the text, so two people named "Alex Kim" each get the one picked (W18-2).
  const pickedRefs = keptPicks(text, picked);
  const pickedTokens = new Set(picked.map((pick) => pick.token.toLowerCase()));
  const everyone = [...members, ...outsiders];
  const wasPicked = (ref: MemberRef) => pickedRefs.some((pick) => sameMember(pick, ref));
  // A token someone was picked for resolves to the pick, never to another person who shares it.
  const fromText = (people: readonly MentionTarget[]) => resolveTypedMentions(text, people).filter((ref) => wasPicked(ref)
    || !everyone.some((person) => sameMember(person, ref) && [person.displayName, person.mentionHandle].some((token) => !!token && pickedTokens.has(token.toLowerCase()))));
  const isMember = (ref: MemberRef) => members.some((member) => sameMember(member, ref));
  const mentions = uniqueRefs([...pickedRefs.filter(isMember), ...fromText(members)]);
  // Named with @ but not here: asked about on send (W18-3).
  const outsideMentions = canInvite ? uniqueRefs([...pickedRefs, ...fromText(outsiders)].filter((ref) => !isMember(ref))) : [];
  const [invite, setInvite] = useState<{ people: MemberRef[]; error?: string; adding?: boolean } | null>(null);
  const { ids: sessionMentions, unresolved: staleHandles } = resolveTypedSessionMentions(text, sessionTargets.map(({ candidate }) => candidate), selectedSessions);
  const staleKey = staleHandles.join(" ");
  const authorized = mentions.every((mention) => members.some((member) => sameMember(member, mention)));
  const bytes = new TextEncoder().encode(text).length;
  const error = bytes > 100_000
    ? "Keep your message within 100,000 UTF-8 bytes."
    : mentions.length > 32
      ? "Choose at most 32 room members."
      : sessionMentions.length > 4
        ? "Choose at most four sessions."
        : !authorized
          ? "A selected mention is no longer in this room."
          : "";
  const effectiveState = sendState === "idle" ? lastEvidence : sendState;
  const uncertain = effectiveState === "uncertain" || callbackUncertain;
  const pending = effectiveState === "sending" || (effectiveState === "idle" && submitted && !uncertain);
  const uploading = attachments.some((item) => !item.attachment && !item.error);
  const uploadFailed = attachments.some((item) => item.error);
  const trimmed = text.trim();
  // A draft that is exactly a pre-typed chip prefix is unfinished whatever is attached:
  // the onboarding capture reads the text alone and is one-shot.
  const incompleteDraft = !!placeholderDrafts?.includes(trimmed);
  const hasContent = !incompleteDraft && (!isBlankMarkdown(text) || attachments.some((item) => item.attachment));
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

  function request(extra: MemberRef[] = []): CreateMessageRequest {
    return { clientMessageId, text, mentions: [...mentions, ...extra], ...(sessionMentions.length ? { sessionMentions } : {}), replyToMessageId, threadRootMessageId, attachmentIds: attachments.flatMap((item) => item.attachment ? [item.attachment.id] : []) };
  }

  function supportedType(file: File): MessageAttachmentContentType | undefined {
    return resolveMessageAttachmentContentType(file.name, file.type);
  }

  function validationError(file: File): string | undefined {
    const type = supportedType(file);
    if (!type) return "Unsupported file type or mismatched filename. Attach an image, PDF, ZIP, Office (XLSX/DOCX/PPTX), or text (CSV/TXT/MD) file.";
    if (file.size > MESSAGE_ATTACHMENT_MAX_BYTES[type]) {
      const kind = type === "application/pdf" ? "PDFs" : type === "application/zip" ? "ZIP files" : type.startsWith("image/") ? "Images" : "Files";
      const mib = Math.round(MESSAGE_ATTACHMENT_MAX_BYTES[type] / (1024 * 1024));
      return `${kind} must be ${mib} MiB or smaller.`;
    }
    if (!file.size) return "This file is empty.";
  }

  function uploadError(reason: unknown): string {
    if (reason instanceof ApiError && reason.status === 400) {
      return "The server rejected this file. Check that its contents match its type and the file is within the size limit.";
    }
    if (!(reason instanceof Error)) return "Upload failed. Try again.";
    return reason.message || "Upload failed. Try again.";
  }

  function uploadItem(key: number, file: File) {
    if (!onUploadAttachment) return;
    setAttachments((current) => current.map((item) => item.key === key ? { ...item, attachment: undefined, error: undefined } : item));
    void onUploadAttachment(file).then(
      (attachment) => {
        const type = supportedType(file);
        const mismatch = attachment.contentType !== type || attachment.size !== file.size;
        setAttachments((current) => current.map((item) => item.key === key
          ? mismatch ? { ...item, error: "The server returned different file metadata. Remove this file and try again." } : { ...item, attachment }
          : item));
      },
      (reason) => setAttachments((current) => current.map((item) => item.key === key ? { ...item, error: uploadError(reason) } : item)),
    );
  }

  function addAttachments(files: File[]) {
    if (!onUploadAttachment || !editable) return;
    setAttachmentError("");
    const room = Math.max(0, MESSAGE_ATTACHMENT_MAX_COUNT - attachments.length);
    if (files.length > room) setAttachmentError(`Attach at most ${MESSAGE_ATTACHMENT_MAX_COUNT} files per message.`);
    for (const file of files.slice(0, room)) {
      const key = nextAttachmentKey.current++;
      const preview = file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined;
      if (preview) previews.current.add(preview);
      const error = validationError(file);
      setAttachments((current) => [...current, { key, file, preview, error }]);
      if (!error) uploadItem(key, file);
    }
  }

  function removeAttachment(key: number) {
    if (!editable) return;
    const preview = attachments.find((item) => item.key === key)?.preview;
    if (preview) {
      URL.revokeObjectURL(preview);
      previews.current.delete(preview);
    }
    setAttachments((current) => current.filter((item) => item.key !== key));
    setAttachmentError("");
  }

  // Refs can't be written during render (React may call render without
  // committing) — keep the ref current after every commit instead, so the
  // dropTargetRef listener effect below (bound once) always calls the latest
  // addAttachments via the ref rather than a stale closure.
  useEffect(() => {
    addAttachmentsRef.current = addAttachments;
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
    const files = [...(event.dataTransfer?.files ?? [])];
    if (files.length) addAttachments(files);
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
      const files = [...(event.dataTransfer?.files ?? [])];
      if (files.length) addAttachmentsRef.current(files);
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
    // addAttachments is read through a ref so the listeners bind once and never see stale state
  }, [dropTargetRef]);

  // The box reports the `@que`/`:emo` at the caret as it changes.
  const syncTrigger = useCallback((next: CompletionTrigger | null) => {
    setTrigger(next);
    setActiveIndex(0);
  }, []);

  function choose(option: MentionOption) {
    if (!trigger) return;
    // A person by name (their handle comes from their email, W18-2); an agent by its handle.
    const token = option.kind === "session" ? `@${option.option.candidate.handle}`
      : option.member.kind === "agent" && option.member.mentionHandle ? `@${option.member.mentionHandle}` : `@${option.member.displayName}`;
    if (option.kind === "session") setSelectedSessions((current) => ({ ...current, [option.option.candidate.handle.toLowerCase()]: option.option.candidate.session.sessionId }));
    else setPicked((current) => [...current, { token: token.slice(1), ref: { kind: option.member.kind, id: option.member.id } }]);
    setTrigger(null);
    field.current?.replaceTrigger(token);
  }

  function insertAtCaret(value: string) {
    setTrigger(null);
    field.current?.insertText(value);
  }

  function chooseEmoji(emoji: string) {
    if (!trigger) return;
    recordRecentEmoji(emoji);
    setTrigger(null);
    field.current?.replaceTrigger(emoji);
  }

  function submit(retry = false, restoreFocus = false, sendStale = false, added: MemberRef[] | "skip" | null = null) {
    if (retry
      ? disabled || !!error || uploading || uploadFailed || !hasContent || !clientMessageId || effectiveState !== "rejected" || !onRetry || retryLocked.current
      : !canSend || locked.current) return;
    // A handle that stopped matching would otherwise go out as plain text without a word.
    if (!retry && staleKey && !sendStale) {
      setStaleNotice(staleKey);
      return;
    }
    // Slack: "Priya isn't in #x — Add them / Do nothing".
    if (!retry && outsideMentions.length > 0 && added === null) {
      setInvite({ people: outsideMentions });
      return;
    }
    setInvite(null);
    setStaleNotice("");
    locked.current = true;
    if (restoreFocus) requestFocusAfterSend();
    if (retry) {
      retryLocked.current = true;
      setRetryPending(true);
    }
    setSubmitted(true);
    // Only a composer that sends attachments uses them up (a request reply leaves them for the room).
    if (onUploadAttachment) carried.current.sent = true;
    onTypingChange?.(false);
    const version = evidenceVersion.current;
    const markUncertain = () => {
      cancelFocusAfterSend();
      if (version !== evidenceVersion.current) return;
      setCallbackUncertain(true);
      setRetryPending(false);
    };
    try {
      void Promise.resolve((retry ? onRetry! : onSubmit)(request(Array.isArray(added) ? added : []))).catch(markUncertain);
    } catch {
      markUncertain();
    }
  }

  async function addAndSend() {
    if (!invite || !onAddPeople || invite.adding) return;
    setInvite({ ...invite, adding: true, error: undefined });
    try {
      await onAddPeople(invite.people);
    } catch (reason) {
      setInvite({ people: invite.people, error: reason instanceof Error && reason.message ? reason.message : "They could not be added. Try again." });
      return;
    }
    submit(false, true, true, invite.people);
  }

  // Formatting applies in the box as you work, like Slack (W18-4); the message still goes out as markdown.
  const [linkDraft, setLinkDraft] = useState<string | null>(null);
  function format(style: MarkdownStyle) {
    if (style !== "link") { field.current?.format(style); return; }
    if (field.current?.selectedLink()) { field.current.link(null); return; }
    setLinkDraft("");
  }
  function applyLink() {
    const url = linkDraft?.trim();
    setLinkDraft(null);
    if (!url) { field.current?.focus(); return; }
    // Nothing selected: the address goes in as its own link text.
    if (!field.current?.link(url)) field.current?.insertText(url);
  }

  const hint = showRespondHint ? respondHint(roomKind, members, mentions, runningAgentIds, true) : null;
  // Who-will-reply matters at the moment of sending, not while the box is empty.
  const showHint = !!hint && hasContent;

  // The box's parts, laid out as one line (super-agent) or as the classic two rows.
  function handleKey(event: KeyboardEvent): boolean {
    const composingKey = event.isComposing || event.keyCode === 229;
    if (listOpen) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (composingKey) return false;
        const count = mentionOpen ? options.length : emojiOptions.length;
        setActiveIndex((index) => (index + (event.key === "ArrowDown" ? 1 : count - 1)) % count);
        return true;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        if (composingKey) return false;
        if (mentionOpen) choose(options[activeIndex] ?? options[0]);
        else chooseEmoji((emojiOptions[activeIndex] ?? emojiOptions[0]).emoji);
        return true;
      }
      if (event.key === "Escape") {
        // Esc closes the typeahead FIRST; the thread/overlay listener on window must not also fire.
        event.stopImmediatePropagation();
        setTrigger(null);
        return true;
      }
    }
    // Plain ArrowUp only — ⌥↑ is the window-level previous-room shortcut,
    // and swallowing it here would make it dead from the composer.
    if (event.key === "ArrowUp" && !event.altKey && isBlankMarkdown(text) && onEditLast) {
      onEditLast();
      return true;
    }
    if (!listOpen && (event.metaKey || event.ctrlKey)) {
      const key = event.key.toLowerCase();
      const style: MarkdownStyle | null = event.shiftKey
        ? key === "x" ? "strike" : key === "c" ? "codeblock" : null
        : key === "b" ? "bold" : key === "i" ? "italic" : null;
      if (style) { format(style); return true; }
    }
    if (event.key === "Enter" && !event.shiftKey && !composingKey) {
      submit(false, true);
      return true;
    }
    return false;
  }

  const boxPlaceholder = placeholder ?? (label === "Reply to thread" ? "Reply to thread…" : "Ask Lightbulb…");
  const Editor = editorLoaded ? LoadedEditor : null;
  const textarea = !Editor ? <textarea id={fieldId} className="room-editor room-editor-plain" rows={1} aria-label={label} placeholder={boxPlaceholder}
    value={text} disabled={!editable} aria-describedby={`${fieldId}-help ${fieldId}-status`}
    onFocus={() => { plainFocused.current = true; }} onBlur={() => { plainFocused.current = false; onTypingChange?.(false); }}
    onChange={(event) => { onTextChange(event.target.value); onTypingChange?.(!!event.target.value.trim()); }}
    onKeyDown={(event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); submit(false, true); }
    }} />
  : <Editor handleRef={field} id={fieldId} value={text} editable={editable} label={label} autoFocus={plainFocused.current}
    placeholder={boxPlaceholder}
    ariaProps={{
      "aria-invalid": !!error,
      "aria-describedby": `${fieldId}-help ${fieldId}-status${showHint ? ` ${fieldId}-respond` : ""}`,
      "aria-autocomplete": "list", "aria-expanded": listOpen, "aria-controls": listOpen ? `${fieldId}-targets` : undefined,
      "aria-activedescendant": listOpen ? `${fieldId}-option-${activeIndex}` : undefined,
    }}
    onChange={(markdown) => { onTextChange(markdown); onTypingChange?.(!!markdown.trim()); }}
    onTrigger={syncTrigger} onKeyDown={handleKey} onPasteFiles={addAttachments} onBlur={() => onTypingChange?.(false)} />;
  const fileInput = onUploadAttachment && <input ref={attachmentInput} className="sr-only" type="file" accept={MESSAGE_ATTACHMENT_ACCEPT} multiple
    onChange={(event) => { addAttachments([...event.target.files ?? []]); event.target.value = ""; }} />;
  const formatButton = <Button type="button" variant="ghost" size="icon-sm" aria-label={formatOpen ? "Hide formatting" : "Show formatting"} aria-pressed={formatOpen}
    aria-controls={formatOpen ? `${fieldId}-format` : undefined} onClick={() => setFormatOpen(!formatOpen)}><CaseSensitive aria-hidden="true" /></Button>;
  const mentionButton = <Button type="button" variant="ghost" size="icon-sm" aria-label="Mention someone" aria-expanded={mentionOpen}
    disabled={!editable} onClick={() => field.current?.insertText(text && !/\s$/.test(text) ? " @" : "@")}><AtSign aria-hidden="true" /><span className="sr-only">Mention</span></Button>;
  const sendButton = <Button type="submit" size="icon-sm" className="room-send" disabled={!canSend} aria-label="Send message"><Send aria-hidden="true" /><span className="sr-only">Send</span></Button>;

  return <form className="room-composer" data-compact={compact || undefined} aria-label={`${label} composer`} aria-busy={pending || retryPending}
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
        // The box keeps focus and its selection while the bar is clicked.
        onMouseDown={(event) => event.preventDefault()}
        onFocus={() => setToolbarIndex(index)} onClick={() => format(style)}><Icon aria-hidden="true" /></Button>)}
      {linkDraft !== null && <input autoFocus type="url" className="room-format-link" aria-label="Link address" placeholder="Paste a link, then Enter"
        value={linkDraft} onChange={(event) => setLinkDraft(event.target.value)} onBlur={() => setLinkDraft(null)}
        onKeyDown={(event) => {
          if (event.key === "Enter") { event.preventDefault(); applyLink(); }
          if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setLinkDraft(null); field.current?.focus(); }
        }} />}
    </div>}
    {dropping && <p className="room-drop-hint text-ui text-muted-foreground" role="status">Drop files to attach</p>}
    <label className="sr-only" htmlFor={fieldId}>{label}</label>
    {mentionOpen && <TypeaheadList id={`${fieldId}-targets`} label={sessionTargets.length ? "Mention a room member or session" : "Mention a room member"} items={options} activeIndex={activeIndex}
      optionId={(index) => `${fieldId}-option-${index}`} onHover={setActiveIndex} onPick={(index) => choose(options[index])}
      renderRow={(entry) => entry.kind === "member" ? <>
        <ParticipantAvatar participant={entry.member} size="sm" />
        <span className="min-w-0 flex-1 text-left"><span className="block truncate" title={entry.member.displayName}>{entry.member.displayName}</span>
          <span className="text-muted-foreground text-ui">{entry.outside ? `Not in ${roomLabel.startsWith("#") ? "channel" : "this chat"}` : entry.member.kind === "agent" && entry.member.mentionHandle ? `@${entry.member.mentionHandle}` : "Person"}</span></span>
      </> : <>
        <span className="flex size-8 items-center justify-center rounded-md bg-muted" aria-hidden="true"><SessionToolIcon provider={entry.option.candidate.session.provider} /></span>
        <span className="min-w-0 flex-1 text-left"><span className="block truncate" title={sessionTitle(entry.option.candidate.session)}>{sessionAgentName(entry.option.candidate.session.provider)} · {sessionTitle(entry.option.candidate.session)}</span>
          <span className="block truncate text-muted-foreground text-ui">{entry.option.ownerName}{entry.option.candidate.collided ? ` · ${entry.option.candidate.session.cwdName ? `${entry.option.candidate.session.cwdName} · ` : ""}${entry.option.candidate.session.sessionId.slice(0, 8)}` : ""}</span></span>
      </>} />}
    {emojiOpen && <TypeaheadList id={`${fieldId}-targets`} label="Insert an emoji" items={emojiOptions} activeIndex={activeIndex}
      optionId={(index) => `${fieldId}-option-${index}`} onHover={setActiveIndex} onPick={(index) => chooseEmoji(emojiOptions[index].emoji)}
      renderRow={(entry) => <><span aria-hidden="true" className="text-lg">{entry.emoji}</span><span className="flex-1 text-left truncate">{entry.name}</span></>} />}
    {!compact && textarea}
    {attachments.length > 0 && <div className="room-attachment-previews">{attachments.map((item) => item.preview
      ? <div key={item.key} className="room-image-preview">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={item.preview} alt={item.file.name} />
        <Button type="button" variant="ghost" aria-label={`Remove ${item.file.name}`} disabled={!editable} onClick={() => removeAttachment(item.key)}><X aria-hidden="true" /></Button>
        <span className={item.error ? "text-err" : "text-muted-foreground"}>{item.error ?? (item.attachment ? "Ready" : "Uploading…")}</span>
      </div>
      : <div key={item.key} className="room-file-preview">
        <FileText aria-hidden="true" />
        <span className="min-w-0 flex-1"><strong className="block truncate" title={item.file.name}>{item.file.name}</strong>
          <span className="block text-ui text-muted-foreground">{supportedType(item.file) ?? (item.file.type || "Unknown type")} · {formatBytes(item.file.size)}</span>
          <span role="status" className={item.error ? "block text-ui text-err" : "block text-ui text-muted-foreground"}>{item.error ?? (item.attachment ? "Ready" : "Uploading…")}</span></span>
        {item.error && !validationError(item.file) && <Button type="button" variant="ghost" size="icon-sm" aria-label={`Retry ${item.file.name}`} disabled={!editable} onClick={() => uploadItem(item.key, item.file)}><RotateCcw aria-hidden="true" /></Button>}
        <Button type="button" variant="ghost" size="icon-sm" aria-label={`Remove ${item.file.name}`} disabled={!editable} onClick={() => removeAttachment(item.key)}><X aria-hidden="true" /></Button>
      </div>)}</div>}
    {attachmentError && <p role="alert" className="text-ui text-err">{attachmentError}</p>}
    {invite && <div role="alert" className="room-invite-prompt text-ui">
      <span>{invite.people.map((ref) => outsiders.find((person) => sameMember(person, ref))?.displayName ?? "Someone").join(", ")} {invite.people.length === 1 ? "isn't" : "aren't"} in {roomLabel}.</span>
      <Button type="button" size="sm" disabled={invite.adding} onClick={() => void addAndSend()}>{invite.adding ? "Adding…" : invite.people.length === 1 ? "Add them" : "Add them all"}</Button>
      <Button type="button" size="sm" variant="ghost" disabled={invite.adding} onClick={() => submit(false, true, true, "skip")}>Do nothing</Button>
      {invite.error && <span className="text-err">{invite.error}</span>}
    </div>}
    {staleKey && staleNotice === staleKey && <p role="alert" className="text-ui text-warn">
      {staleHandles.map((handle) => `@${handle}`).join(", ")} no longer {staleHandles.length === 1 ? "matches" : "match"} a shared session. Remove it or send anyway.{" "}
      <Button type="button" variant="link" size="sm" className="h-auto p-0" onClick={() => submit(false, true, true)}>Send anyway</Button></p>}
    {compact ? <div className="room-composer-row">
      {fileInput}
      {/* + : attach, and the emoji picker (moved out of the row). */}
      <EmojiPicker open={pickerOpen} onOpenChange={setPickerOpen} onPick={insertAtCaret} anchor={<span className="room-composer-plus">
        <DropdownMenu>
          <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon-sm" aria-label="Add" title="Add" disabled={!editable}><Plus aria-hidden="true" /></Button></DropdownMenuTrigger>
          <DropdownMenuContent align="start" side="top" onCloseAutoFocus={(event) => { if (emojiPending.current) { event.preventDefault(); emojiPending.current = false; } }}>
            {onUploadAttachment && <DropdownMenuItem disabled={attachments.length >= MESSAGE_ATTACHMENT_MAX_COUNT} onSelect={() => attachmentInput.current?.click()}>
              <Paperclip aria-hidden="true" />Attach files</DropdownMenuItem>}
            <DropdownMenuItem onSelect={() => { emojiPending.current = true; setPickerOpen(true); }}><SmilePlus aria-hidden="true" />Emoji</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </span>} />
      {textarea}
      <div className="room-composer-actions">{formatButton}{mentionButton}{sendButton}</div>
    </div> : <div className="room-composer-actions">
      {onUploadAttachment && <>{fileInput}
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Attach files" disabled={!editable || attachments.length >= MESSAGE_ATTACHMENT_MAX_COUNT} onClick={() => attachmentInput.current?.click()}><Paperclip aria-hidden="true" /></Button></>}
      {formatButton}
      <EmojiPicker triggerLabel="Insert emoji" disabled={!editable} onPick={insertAtCaret} />
      {mentionButton}
      {sendButton}
    </div>}
    {/* The line is held while the box is empty, so the bar above never moves when the hint appears (W18-4). */}
    {(showHint || (compact && hint)) && <p id={`${fieldId}-respond`} className="room-respond-hint text-ui text-muted-foreground" data-held={!showHint || undefined}>{hint}</p>}
    <p id={`${fieldId}-help`} className={error ? "text-ui text-err" : "sr-only"}>{error || "Enter to send · Shift+Enter for a new line."}</p>
    <p id={`${fieldId}-status`} role="status" className={uncertain ? "text-ui text-warn" : effectiveState === "rejected" ? "text-ui text-err" : "text-ui text-muted-foreground"}>{status}</p>
    {uncertain && onReconcile && <Button type="button" variant="outline" disabled={disabled} onClick={() => onReconcile(clientMessageId)}>Check message status</Button>}
    {!uncertain && effectiveState === "rejected" && onRetry && <Button type="button" variant="outline" disabled={disabled || !!error || uploading || (!text.trim() && !attachments.some((item) => item.attachment)) || retryPending} onClick={() => submit(true)}>Retry message</Button>}
  </form>;
}
