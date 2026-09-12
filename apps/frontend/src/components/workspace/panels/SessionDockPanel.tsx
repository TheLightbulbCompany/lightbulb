"use client";

import { useEffect, useMemo, useState } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import type { RoomMember } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useRoomResources } from "@/hooks/useRoomResources";
import { LocalSessionPanel, sessionLabel } from "@/components/messaging/LocalSessionPanel";
import { useDockPanelParams } from "./useDockPanelParams";

interface SessionDockPanelParams { roomId: string; sessionId: string }

export function SessionDockPanel(props: IDockviewPanelProps<SessionDockPanelParams>): React.ReactElement {
  const params = useDockPanelParams(props);
  const { messaging, onEvent, connectionRevision } = useLiveEvents();
  const source = useMemo(() => ({ client: messaging, onEvent, connectionRevision }), [messaging, onEvent, connectionRevision]);
  const resources = useRoomResources(params.roomId, source);
  const [members, setMembers] = useState<RoomMember[]>([]);
  useEffect(() => {
    let alive = true;
    messaging.getRoom(params.roomId).then(room => { if (alive) setMembers(room.members); }).catch(() => { if (alive) setMembers([]); });
    return () => { alive = false; };
  }, [messaging, params.roomId, resources.accessRevision]);
  const session = resources.data?.sessions.find(s => s.sessionId === params.sessionId);
  const title = session ? sessionLabel(session) : "Session";
  useEffect(() => { props.api.setTitle(title); }, [props.api, title]);
  const owner = members.find(m => m.kind === "human" && m.id === session?.humanUserId)?.displayName ?? "Room member";
  if (!session) {
    return <div className="p-4" role={resources.error ? "alert" : "status"}>
      <p>{resources.error ?? (resources.loading ? "Loading session…" : "Session unavailable in this conversation.")}</p>
      {!resources.loading && <Button variant="outline" onClick={() => void resources.refresh()}>Try again</Button>}
    </div>;
  }
  return <div className="native-session-dock room-context-body" style={{ display: "flex", flexDirection: "column", height: "100%", padding: 0 }}>
    <LocalSessionPanel key={`${session.sessionId}:${session.generation}:${resources.accessRevision}`}
      session={session} ownerName={owner} roomId={params.roomId} client={source.client} revision={resources.revision}
      onAccessLost={resources.revoke} participants={members} />
  </div>;
}
