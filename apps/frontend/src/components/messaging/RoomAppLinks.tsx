"use client";

import { useMemo, type MouseEvent, type ReactNode } from "react";
import { Hash, MessageSquareQuote, SquareTerminal } from "lucide-react";
import { roomDisplayName, sessionLabel } from "@lightbulb/shared";
import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick } from "@/lib/shellNav";
import { extractAppLinks, type AppLink } from "./appLinks";
import { useMessagingWorkspaceOptional, type MessagingWorkspaceValue } from "./MessagingWorkspaceContext";
import { ParticipantAvatar } from "./ParticipantAvatar";

/**
 * Links to our own app, shown as the native thing they point at instead of the
 * site's OG card. Issues and documents are rendered by MessageTaskCard and
 * RoomDocumentCards; this covers approvals, agents, rooms, messages and
 * sessions. Anything it cannot resolve from data already loaded renders
 * nothing — the link in the text stays a plain (in-app) link.
 */
export function RoomAppLinks({ text }: { text: string }) {
  const links = useMemo(() => extractAppLinks(text).filter((l) => !["issue", "document", "other"].includes(l.kind)), [text]);
  const ws = useMessagingWorkspaceOptional();
  if (!links.length) return null;
  return <div className="room-link-previews">{links.map((link) => <AppLinkCard key={link.href} link={link} ws={ws} />)}</div>;
}

/** Open an own-app link in place: room/message/session through the workspace, the rest via the shell URL. */
export function openAppLink(event: MouseEvent<HTMLAnchorElement>, link: AppLink, ws: MessagingWorkspaceValue | null): void {
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  if (ws && (link.kind === "room" || link.kind === "message")) {
    event.preventDefault();
    ws.openMessage(link.roomId, link.kind === "message" ? link.messageId : null);
  } else if (ws && link.kind === "session") {
    event.preventDefault();
    ws.openSession(link.sessionId);
  } else if (link.href.startsWith("/chat") && window.location.pathname === "/chat") {
    handleShellLinkClick(event, link.href);
  }
}

function AppLinkCard({ link, ws }: { link: AppLink; ws: MessagingWorkspaceValue | null }) {
  if (link.kind === "approval") return <ApprovalCard id={link.id} />;
  if (!ws) return null;
  const rooms = ws.directoryProps.rooms;
  const chip = (icon: ReactNode, label: string, meta?: string) =>
    <a className="room-app-chip" href={link.href} onClick={(e) => openAppLink(e, link, ws)}>
      {icon}<span className="room-app-chip-label">{label}</span>{meta && <span className="room-link-card-meta">{meta}</span>}
    </a>;
  if (link.kind === "agent") {
    const agent = ws.directoryProps.participants.find((p) => p.kind === "agent" && (p.id === link.id || p.mentionHandle === link.id));
    return agent ? chip(<ParticipantAvatar participant={agent} size="xs" showPresence={false} />, agent.displayName, "Agent") : null;
  }
  if (link.kind === "session") {
    const session = ws.workspaceSessions.sessions.find((s) => s.sessionId === link.sessionId);
    return session ? chip(<SquareTerminal size={14} aria-hidden="true" />, sessionLabel(session), "Session") : null;
  }
  if (link.kind !== "room" && link.kind !== "message") return null;
  const room = rooms.find((r) => r.roomId === link.roomId);
  if (!room) return null;
  const roomName = roomDisplayName(room, room.participants);
  if (link.kind === "room") return chip(<Hash size={14} aria-hidden="true" />, roomName);
  const quoted = ws.roomProps?.room?.roomId === link.roomId ? ws.roomProps.messages.find((m) => m.messageId === link.messageId) : undefined;
  if (!quoted || quoted.deletedAt) return chip(<MessageSquareQuote size={14} aria-hidden="true" />, `Message in ${roomName}`);
  return <a className="room-link-card room-quote-card" href={link.href} onClick={(e) => openAppLink(e, link, ws)}>
    <span className="room-link-card-body">
      <span className="room-link-card-title">{quoted.author.displayName || "Unknown author"}</span>
      <span className="room-link-card-description">{quoted.text}</span>
      <span className="room-link-card-meta">{roomName} · Jump to message</span>
    </span>
  </a>;
}

function ApprovalCard({ id }: { id: string }) {
  const { read } = useTeamsApi();
  const { data } = read<Approval>(`/approvals/${encodeURIComponent(id)}`);
  return data?.id ? <section aria-label="Approval in this message" className="editorial-card max-w-xl"><ApprovalRow approval={data} /></section> : null;
}
