"use client";

import { useState } from "react";
import type { SessionRow, SessionRowState } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { bridge } from "./localTerminal";
import { SessionToolIcon } from "./SessionToolIcon";

const STATE_WORDS: Partial<Record<SessionRowState, string>> = {
  offline: "offline", parked: "parked, wakes when messaged", ended: "ended", waiting: "waiting for you",
};

/** A session only the network knows: the engine lists it, the backend does not, so there is nothing to open. */
export function NetworkSessionRow({ row }: { row: SessionRow }) {
  const [refusal, setRefusal] = useState<string>();
  // `own`, not `isViewer`: the viewer's other Mac is theirs too, and this Mac cannot share its sessions.
  const shareable = row.engine?.own === true && row.agent === "claude" && !["offline", "parked", "ended"].includes(row.state);
  return <li className="room-directory-row room-directory-row--roster session-row session-row--gone">
    <div className="room-directory-entry room-directory-entry--inert">
    <span className="session-tool-badge" aria-hidden="true"><SessionToolIcon provider={row.agent} /></span>
    <span className="min-w-0 flex-1">
      <span className="block truncate" title={row.title}>{row.title || row.agent}</span>
      <span className="block truncate text-caption text-muted-foreground">{row.person.isViewer ? "You" : row.person.name} · {row.folder ?? "on the network"} · {STATE_WORDS[row.state] ?? row.state}</span>
      {refusal && <span role="alert" className="block text-caption text-destructive">{refusal}</span>}
    </span>
    </div>
    {shareable && <Button variant="ghost" size="sm" aria-label={`Share ${row.title || row.agent}`}
      onClick={() => {
        setRefusal(undefined);
        void Promise.resolve(bridge()?.("share_engine_session", { sessionId: row.id })).catch((error: unknown) => {
          const text = String(error instanceof Error ? error.message : error);
          // The person pressed No: nothing to say.
          if (!/declined on this Mac/i.test(text)) setRefusal(text);
        });
      }}>Share</Button>}
  </li>;
}
