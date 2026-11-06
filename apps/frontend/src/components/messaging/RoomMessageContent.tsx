"use client";

import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { MoreHorizontal } from "lucide-react";
import type { CanonicalRoomMessage, UpdateMessageRequest } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";

export function RoomMessageContent({ message, currentUserId, actionable, onUpdate, startEditing = false, onEditingClosed, actions, menuExtras, children }: {
  message: CanonicalRoomMessage;
  currentUserId: string;
  actionable: boolean;
  onUpdate?: (messageId: string, request: UpdateMessageRequest) => Promise<void>;
  startEditing?: boolean;
  // Fires whenever the edit form started by startEditing closes (save/cancel/Escape), so a
  // parent tracking "which message is the ArrowUp edit target" can drop it -- otherwise the
  // target id never changes and a second ArrowUp on the same still-last message is a same-value
  // setState the parent bails on, and the render-phase startEditing guard below never re-fires.
  onEditingClosed?: () => void;
  children: ReactNode;
  actions?: ReactNode;
  // DropdownMenuItems rendered above Edit/Delete. Available to every member (pin,
  // mark-unread-from-here), so their presence opens the "more" menu even for a
  // message the viewer did not write.
  menuExtras?: ReactNode;
}) {
  const id = useId();
  const [request, setRequest] = useState<UpdateMessageRequest | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string>();
  const trigger = useRef<HTMLButtonElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const bar = useRef<HTMLDivElement>(null);
  // One tab stop per message (dev walk W7: Tab walked 8 buttons on every message): "Message actions" when there is
  // one, else the first button; the arrow keys move along the toolbar. Re-applied each render (the buttons change).
  const stop = useRef<number | null>(null);
  useEffect(() => { roveTo(bar.current, stop.current); });
  const onBarKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const buttons = barButtons(bar.current);
    const at = buttons.indexOf(event.target as HTMLButtonElement);
    if (at < 0) return;
    const next = event.key === "ArrowRight" ? (at + 1) % buttons.length : event.key === "ArrowLeft" ? (at - 1 + buttons.length) % buttons.length
      : event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    stop.current = next;
    roveTo(bar.current, next);
    buttons[next].focus();
  };
  const ownMessage = message.author.kind === "human" && message.author.id === currentUserId && !message.deletedAt;
  const [editRequested, setEditRequested] = useState(startEditing);
  if (editRequested !== startEditing) {
    setEditRequested(startEditing);
    if (startEditing && ownMessage && onUpdate && request === null) {
      setRequest({ operationId: crypto.randomUUID(), expectedRevision: message.messageRevision ?? 0, text: message.text });
    }
  }
  const editing = ownMessage && request !== null && request.text !== null;
  const deleting = ownMessage && request?.text === null;
  function restoreFocus() { (trigger.current ?? root.current)?.focus(); }
  function begin(text: string | null) {
    setError(undefined);
    setRequest({ operationId: crypto.randomUUID(), expectedRevision: message.messageRevision ?? 0, text });
  }
  async function save() {
    if (!request || !ownMessage || !actionable || pending || !onUpdate) return;
    setPending(true);
    setError(undefined);
    try {
      await onUpdate(message.messageId, request);
      setRequest(null);
      restoreFocus();
      onEditingClosed?.();
    } catch (error) {
      setError(error instanceof Error ? error.message : "Could not update message. Try again.");
    } finally {
      setPending(false);
    }
  }
  return <div ref={root} tabIndex={-1}>
    {message.deletedAt ? <p className="text-ui text-muted-foreground italic">Message deleted</p> : editing ? <form className="grid gap-2" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <label htmlFor={id} className="sr-only">Edit message</label>
      <textarea id={id} className="room-input min-h-24 w-full" autoFocus value={request!.text!} disabled={pending}
        onChange={(event) => setRequest({ ...request!, operationId: crypto.randomUUID(), text: event.target.value })}
        onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); if (!pending) { setRequest(null); restoreFocus(); onEditingClosed?.(); } } }} />
      <p className="text-ui text-muted-foreground">Edits do not change requests already sent to agents or undo their work.</p>
      {error && <p role="alert" className="text-ui text-err break-words">{error}</p>}
      <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" disabled={pending} onClick={() => { setRequest(null); restoreFocus(); onEditingClosed?.(); }}>Cancel edit</Button>
        <Button type="submit" disabled={!actionable || pending || (!request!.text!.trim() && !message.attachments?.length)}>{pending ? "Saving…" : "Save changes"}</Button></div>
    </form> : <>{children}{message.editedAt && <span className="text-ui text-muted-foreground"> (edited)</span>}</>}
    {!editing && (actions || menuExtras || (ownMessage && onUpdate)) && <div ref={bar} className="room-message-actions" role="toolbar" aria-label="Message tools" onKeyDown={onBarKey}
      onFocus={(event) => { const at = barButtons(bar.current).indexOf(event.target as unknown as HTMLButtonElement); if (at >= 0) { stop.current = at; roveTo(bar.current, at); } }}>
      {actions}
      {(menuExtras || (ownMessage && onUpdate)) && <DropdownMenu>
      <DropdownMenuTrigger asChild><Button ref={trigger} type="button" variant="ghost" size="icon-sm" disabled={!actionable || pending} aria-label="Message actions"><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end" onEscapeKeyDown={(event) => event.stopPropagation()} onCloseAutoFocus={(event) => { if (request) event.preventDefault(); }}>
        {menuExtras}
        {ownMessage && onUpdate && <>
          <DropdownMenuItem onSelect={() => begin(message.text)}>Edit message</DropdownMenuItem>
          <DropdownMenuItem variant="destructive" onSelect={() => begin(null)}>Delete message</DropdownMenuItem>
        </>}
      </DropdownMenuContent>
    </DropdownMenu>}
    </div>}
    <Dialog open={deleting} onOpenChange={(open) => { if (!open && !pending) setRequest(null); }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onEscapeKeyDown={(event) => { event.stopPropagation(); if (pending) event.preventDefault(); }}
        onOpenAutoFocus={(event) => { event.preventDefault(); cancel.current?.focus(); }}
        onCloseAutoFocus={(event) => { event.preventDefault(); restoreFocus(); }}>
        <DialogHeader><DialogTitle>Delete message?</DialogTitle><DialogDescription>This removes the message and its attachments from the conversation. It does not change requests already sent to agents or undo their work. Thread replies stay available.</DialogDescription></DialogHeader>
        {error && <p role="alert" className="text-ui text-err break-words">{error}</p>}
        <DialogFooter className="gap-2"><Button ref={cancel} type="button" variant="outline" disabled={pending} onClick={() => setRequest(null)}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={!actionable || pending} onClick={() => void save()}>{pending ? "Deleting…" : "Delete message"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}

const barButtons = (bar: HTMLElement | null): HTMLButtonElement[] => bar ? [...bar.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")] : [];

/** Roving tabindex: only button `at` (default: "Message actions", else the first) is in the tab order. */
function roveTo(bar: HTMLElement | null, at: number | null): void {
  const buttons = barButtons(bar);
  if (!buttons.length) return;
  const more = buttons.findIndex((button) => button.getAttribute("aria-label") === "Message actions");
  const pick = at !== null && at < buttons.length ? at : more >= 0 ? more : 0;
  buttons.forEach((button, i) => { button.tabIndex = i === pick ? 0 : -1; });
}
