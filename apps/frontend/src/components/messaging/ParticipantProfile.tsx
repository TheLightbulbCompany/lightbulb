"use client";

import { useState } from "react";
import { MessageCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { ParticipantAvatar } from "./ParticipantAvatar";
import type { RoomParticipant } from "./types";

interface ParticipantProfileProps {
  participant: RoomParticipant;
  currentUserId: string;
  onMessage?: (participant: RoomParticipant) => void | Promise<void>;
  disabled?: boolean;
  className?: string;
}

export function ParticipantProfile({ participant, currentUserId, onMessage, disabled, className }: ParticipantProfileProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const isSelf = participant.kind === "human" && participant.id === currentUserId;
  const joined = !Number.isNaN(Date.parse(participant.joinedAt));

  return <Dialog open={open} onOpenChange={(next) => {
    if (busy) return;
    setOpen(next);
    setError(false);
  }}>
    <DialogTrigger asChild><button type="button" disabled={disabled} title={participant.displayName}
      className={cn("room-author font-semibold truncate text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring", className)}>{participant.displayName}</button></DialogTrigger>
    <DialogContent showCloseButton={!busy} className="w-[calc(100%_-_2rem)] max-w-sm max-h-[calc(100dvh_-_2rem)] overflow-y-auto shadow-none"
      onEscapeKeyDown={(event) => { event.stopPropagation(); if (busy) event.preventDefault(); }}
      onInteractOutside={(event) => { if (busy) event.preventDefault(); }}>
      <ParticipantAvatar participant={participant} size="lg" />
      <div className="min-w-0 space-y-2">
        <DialogTitle className="break-words pr-6">{participant.displayName}</DialogTitle>
        <DialogDescription>{participant.kind === "agent" ? "Agent" : isSelf ? "You" : "Workspace member"}</DialogDescription>
      </div>
      <dl className="space-y-3 text-ui">
        {participant.mentionHandle && <div><dt className="text-muted-foreground">Mention</dt><dd className="break-words">@{participant.mentionHandle}</dd></div>}
        <div><dt className="text-muted-foreground">Role here</dt><dd>{participant.role === "owner" ? "Owner" : participant.role === "admin" ? "Admin" : "Member"}</dd></div>
        {joined && <div><dt className="text-muted-foreground">Joined</dt><dd><time dateTime={participant.joinedAt}>{new Date(participant.joinedAt).toLocaleDateString([], { month: "long", day: "numeric", year: "numeric" })}</time></dd></div>}
      </dl>
      {!isSelf && onMessage && <Button type="button" disabled={disabled || busy} onClick={async () => {
        setBusy(true);
        setError(false);
        try { await onMessage(participant); setOpen(false); }
        catch { setError(true); }
        finally { setBusy(false); }
      }}><MessageCircle size={16} aria-hidden="true" />{busy ? "Opening chat…" : "Message"}</Button>}
      {error && <p role="alert" className="text-ui text-err">Could not open this chat. Try again.</p>}
    </DialogContent>
  </Dialog>;
}
