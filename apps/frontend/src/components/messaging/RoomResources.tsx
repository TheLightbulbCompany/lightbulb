"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, Laptop, Terminal } from "lucide-react";
import { Button } from "@/components/ui/button";
import { sessionLabel, sessionStatus, sessionViewKey, useRoomResources, type RoomResourceSource } from "@lightbulb/shared";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { LocalSessionPanel } from "./LocalSessionPanel";
import type { RoomParticipant } from "./types";

export function RoomResources({ roomId, source, participants, currentUserId, onClose, target, onTargetConsumed, onShare, resizeHandle }: {
  roomId: string; source: RoomResourceSource; participants: readonly RoomParticipant[]; currentUserId?: string; onClose: () => void;
  target?: { sessionId: string; nonce: number } | null; onTargetConsumed?: () => void; onShare?: (url: string) => void;
  resizeHandle?: ReactNode;
}) {
  const [includeEnded, setIncludeEnded] = useState(false);
  // A permalink target is one-shot (RoomPanel clears it via onTargetConsumed
  // once resolved) — flip the toggle on rather than gating the fetch on
  // `target` directly, or the list reverts to hiding ended sessions the
  // instant the target is consumed and the just-opened panel bounces closed.
  if (target && !includeEnded) setIncludeEnded(true);
  const resources = useRoomResources(roomId, source, { includeEnded });
  const [selection, setSelection] = useState<{ id: string; accessRevision: number } | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const lastSelected = useRef<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [handled, setHandled] = useState<number | null>(null);
  if (target && target.nonce !== handled && !resources.loading && (resources.data || resources.error)) {
    const found = !resources.error && resources.data?.sessions.some(session => session.sessionId === target.sessionId);
    setHandled(target.nonce);
    setSelection(found ? { id: target.sessionId, accessRevision: resources.accessRevision } : null);
    setUnavailable(!found);
  }
  useEffect(() => {
    if (target && target.nonce === handled) onTargetConsumed?.();
  }, [target, handled, onTargetConsumed]);
  const selected = selection?.accessRevision === resources.accessRevision
    ? resources.data?.sessions.find(session => session.sessionId === selection.id) : undefined;
  // A revoked selection must not reopen if this session is later shared again.
  if (selection && resources.data && !selected) setSelection(null);
  const hasSelection = !!selected;
  const name = (human: string) => participants.find(p => p.kind === "human" && p.id === human)?.displayName ?? "Room member";
  const humans = new Set([
    ...(resources.data?.devices.map(d => d.humanUserId) ?? []),
    ...(resources.data?.pairedNodes.map(n => n.humanUserId) ?? []),
    ...(resources.data?.sessions.map(s => s.humanUserId) ?? []),
  ]);
  const selectedId = selected?.sessionId;
  useEffect(() => {
    if (selectedId) { heading.current?.focus({ preventScroll: true }); lastSelected.current = selectedId; }
    else {
      const previous = lastSelected.current && buttons.current.get(lastSelected.current);
      if (previous) previous.focus({ preventScroll: true });
      else heading.current?.focus({ preventScroll: true });
    }
  }, [selectedId, resources.accessRevision]);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (selectedId) setSelection(null);
      else onClose();
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [selectedId, onClose]);
  function back() { if (selected) setSelection(null); else onClose(); }
  return <aside className="room-context" data-session={hasSelection || undefined} aria-label="Devices & sessions" onKeyDown={event => {
    if (event.key === "Escape") { event.stopPropagation(); back(); }
  }}>
    {selected && resizeHandle}
    <header className="room-context-header">
      <Button variant="ghost" aria-label={selected ? "Back to devices & sessions" : "Back to conversation"} onClick={back}><ArrowLeft aria-hidden="true" /></Button>
      <h3 ref={heading} tabIndex={-1} className="font-semibold room-wrap">{selected ? sessionLabel(selected) : "Devices & sessions"}</h3>
    </header>
    {resources.error && <div role="alert" className="p-4"><p>{resources.error}</p>
      {resources.data && <p>Showing the last successful snapshot; availability may be stale.</p>}
      <Button variant="outline" onClick={() => void resources.refresh()}>Try again</Button></div>}
    {selected ? <LocalSessionPanel key={sessionViewKey(selected, resources.accessRevision)}
      session={selected} ownerName={name(selected.humanUserId)} own={selected.humanUserId === currentUserId} roomId={roomId} client={source.client} revision={resources.revision} onAccessLost={resources.revoke} onShare={onShare}
      participants={participants} />
      : <div className="room-context-body">
        {unavailable && <p role="status">Session unavailable in this conversation.</p>}
        {resources.loading && <p role="status">Loading devices and sessions…</p>}
        {!resources.loading && !resources.error && humans.size === 0 && <p>No paired devices or shared sessions in this conversation.</p>}
        {[...humans].map(human => {
          const data = resources.data!;
          const macs = new Set([
            ...data.devices.filter(d => d.humanUserId === human).map(d => d.deviceId),
            ...data.pairedNodes.filter(n => n.humanUserId === human && n.clientId === "openclaw-macos").map(n => n.deviceId),
            ...data.sessions.filter(s => s.humanUserId === human).map(s => s.deviceId),
          ]);
          return <section className="room-resource-owner" key={human} aria-label={`${name(human)}'s devices`}>
            <h4 className="font-semibold">{name(human)}</h4>
            {[...macs].map(mac => {
              const nodes = data.pairedNodes.filter(n => n.humanUserId === human && n.macDeviceId === mac);
              const sessions = data.sessions.filter(s => s.humanUserId === human && s.deviceId === mac);
              return <div className="room-resource-device" key={mac}>
                <p className="room-resource-label"><Laptop size={16} aria-hidden="true" />Mac · {mac.slice(0, 8)}</p>
                {nodes.length > 0 && <p className="text-sm text-muted-foreground">Paired {nodes.some(n => n.clientId === "node-host") ? "Mac + associated CLI" : "Mac"} · availability unknown</p>}
                <ul aria-label={`Sessions on Mac ${mac.slice(0, 8)}`}>
                  {sessions.map(session => <li key={session.sessionId}><button type="button" className="room-resource-session" aria-label={`Open ${sessionLabel(session)}`}
                    ref={button => { if (button) buttons.current.set(session.sessionId, button); else buttons.current.delete(session.sessionId); }}
                    onClick={() => setSelection({ id: session.sessionId, accessRevision: resources.accessRevision })}>
                    <span>{sessionLabel(session)}</span><span className="text-sm text-muted-foreground">{sessionStatus[session.status]}{session.lastEventAt ? ` · ${timeAgo(session.lastEventAt)}` : ""}</span>
                  </button></li>)}
                </ul>
                {sessions.length === 0 && <p className="text-sm text-muted-foreground">No shared local sessions.</p>}
              </div>;
            })}
            {data.pairedNodes.filter(n => n.humanUserId === human && n.clientId === "node-host" && !n.macDeviceId).map(node =>
              <div className="room-resource-device" key={node.deviceId}><p className="room-resource-label"><Terminal size={16} aria-hidden="true" />Paired CLI endpoint · {node.deviceId.slice(0, 8)}</p><p className="text-sm text-muted-foreground">Availability unknown · no Mac association verified</p></div>)}
          </section>;
        })}
        {humans.size > 0 && <p className="text-sm text-muted-foreground">Paired endpoints are listed independently of session sharing. Pairing does not confirm they are online or available to run work.</p>}
        <button type="button" className="text-sm text-muted-foreground underline" onClick={() => setIncludeEnded(v => !v)}>
          {includeEnded ? "Hide recent" : "Show recent"}
        </button>
      </div>}
  </aside>;
}
