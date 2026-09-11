"use client";

import { useId, useRef, useState, type ReactNode } from "react";
import { MoreHorizontal } from "lucide-react";
import type { CanonicalRoomMessage, UpdateMessageRequest } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";

export function RoomMessageContent({ message, currentUserId, actionable, onUpdate, actions, children }: {
  message: CanonicalRoomMessage;
  currentUserId: string;
  actionable: boolean;
  onUpdate?: (messageId: string, request: UpdateMessageRequest) => Promise<void>;
  children: ReactNode;
  actions?: ReactNode;
}) {
  const id = useId();
  const [request, setRequest] = useState<UpdateMessageRequest | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string>();
  const trigger = useRef<HTMLButtonElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const ownMessage = message.author.kind === "human" && message.author.id === currentUserId && !message.deletedAt;
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
    } catch (error) {
      setError(error instanceof Error ? error.message : "Could not update message. Try again.");
    } finally {
      setPending(false);
    }
  }
  return <div ref={root} tabIndex={-1}>
    {message.deletedAt ? <p className="text-sm text-muted-foreground italic">Message deleted</p> : editing ? <form className="grid gap-2" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <label htmlFor={id} className="sr-only">Edit message</label>
      <textarea id={id} className="room-input min-h-24 w-full" autoFocus value={request!.text!} disabled={pending}
        onChange={(event) => setRequest({ ...request!, operationId: crypto.randomUUID(), text: event.target.value })}
        onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); if (!pending) { setRequest(null); restoreFocus(); } } }} />
      <p className="text-sm text-muted-foreground">Edits do not change requests already sent to agents or undo their work.</p>
      {error && <p role="alert" className="text-sm text-err break-words">{error}</p>}
      <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" disabled={pending} onClick={() => { setRequest(null); restoreFocus(); }}>Cancel edit</Button>
        <Button type="submit" disabled={!actionable || pending || (!request!.text!.trim() && !message.attachments?.length)}>{pending ? "Saving…" : "Save changes"}</Button></div>
    </form> : <>{children}{message.editedAt && <span className="text-sm text-muted-foreground"> (edited)</span>}</>}
    {!editing && (actions || (ownMessage && onUpdate)) && <div className="room-message-actions" role="group" aria-label="Message actions">
      {actions}
      {ownMessage && onUpdate && <DropdownMenu>
      <DropdownMenuTrigger asChild><Button ref={trigger} type="button" variant="ghost" size="icon-sm" disabled={!actionable || pending} aria-label="Message actions"><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end" onEscapeKeyDown={(event) => event.stopPropagation()} onCloseAutoFocus={(event) => { if (request) event.preventDefault(); }}>
        <DropdownMenuItem onSelect={() => begin(message.text)}>Edit message</DropdownMenuItem>
        <DropdownMenuItem variant="destructive" onSelect={() => begin(null)}>Delete message</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>}
    </div>}
    <Dialog open={deleting} onOpenChange={(open) => { if (!open && !pending) setRequest(null); }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onEscapeKeyDown={(event) => { event.stopPropagation(); if (pending) event.preventDefault(); }}
        onOpenAutoFocus={(event) => { event.preventDefault(); cancel.current?.focus(); }}
        onCloseAutoFocus={(event) => { event.preventDefault(); restoreFocus(); }}>
        <DialogHeader><DialogTitle>Delete message?</DialogTitle><DialogDescription>This removes the message and its attachments from the conversation. It does not change requests already sent to agents or undo their work. Thread replies stay available.</DialogDescription></DialogHeader>
        {error && <p role="alert" className="text-sm text-err break-words">{error}</p>}
        <DialogFooter className="gap-2"><Button ref={cancel} type="button" variant="outline" disabled={pending} onClick={() => setRequest(null)}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={!actionable || pending} onClick={() => void save()}>{pending ? "Deleting…" : "Delete message"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}
