"use client";

import type { SessionRow, SessionRowState } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { bridge } from "./localTerminal";
import { SessionToolIcon } from "./SessionToolIcon";

const STATE_WORDS: Partial<Record<SessionRowState, string>> = {
  offline: "offline", parked: "parked, wakes when messaged", ended: "ended", waiting: "waiting for you",
};

/** A session only the network knows: the engine lists it, the backend does not, so there is nothing to open. */
export function NetworkSessionRow({ row }: { row: SessionRow }) {
  const shareable = row.person.isViewer && row.agent === "claude" && !["offline", "parked", "ended"].includes(row.state);
  return <li className="room-directory-row room-directory-row--roster session-row session-row--gone">
    <div className="room-directory-entry">
    <span className="session-tool-badge" aria-hidden="true"><SessionToolIcon provider={row.agent} /></span>
    <span className="min-w-0 flex-1">
      <span className="block truncate" title={row.title}>{row.title || row.agent}</span>
      <span className="block truncate text-caption text-muted-foreground">{row.person.isViewer ? "You" : row.person.name} · {row.folder ?? "on the network"} · {STATE_WORDS[row.state] ?? row.state}</span>
    </span>
    </div>
    {shareable && <Button variant="ghost" size="sm" aria-label={`Share ${row.title || row.agent}`}
      // The person pressed No, or sharing is off: nothing to say.
      onClick={() => void Promise.resolve(bridge()?.("share_engine_session", { sessionId: row.id })).catch(() => undefined)}>Share</Button>}
  </li>;
}
