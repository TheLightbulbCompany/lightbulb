import { useCallback, useMemo, useState } from "react";

import type { LiveEvent } from "../live/types";
import type { LocalSession } from "./types";
import { usePolledSnapshot, type RoomResourceSource } from "./useRoomResources";

const isSessionsHint = (event: LiveEvent) => event.type === "workspace.sessions.changed";

/**
 * Every live session in the workspace, for the sidebar, the glyph and the
 * mobile list. Polls slowly; the relay's `workspace.sessions.changed` hint on
 * the private user channel is what makes a new session show within a second.
 * `sharing` is who has one: the sharing dot on member lists.
 */
export function useWorkspaceSessions(
  { client, onEvent, connectionRevision }: RoomResourceSource,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const [sessions, setSessions] = useState<LocalSession[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const { sessions: next } = await client.listWorkspaceSessions();
      return () => { setSessions(next); setError(null); };
    } catch {
      return () => setError("Could not refresh shared sessions.");
    }
  }, [client]);
  const { loading, refresh } = usePolledSnapshot(load, { onEvent, connectionRevision }, { enabled, intervalMs: 30000, isHint: isSessionsHint });
  const sharing = useMemo(() => new Set(sessions.map((session) => session.humanUserId)), [sessions]);
  return { sessions, sharing, error, loading, refresh };
}
