import { useEffect, useState } from "react";
import type { MessagingClient, SubagentTail } from "./client";

/** Upstream's transcript pane: the last 16 KB of one subagent, every 2 s while it is open.
 *  Pass `null` to stop polling; the last text stays. */
export function useSubagentTail(client: MessagingClient, target: { roomId: string; agentId: string; subagentId: string } | null) {
  const [tail, setTail] = useState<SubagentTail | null>(null);
  const key = target ? `${target.roomId}/${target.agentId}/${target.subagentId}` : "";
  useEffect(() => {
    if (!target) return; // a cleared target stops polling and keeps the last text: a finished child's final tail
    setTail(null);
    let live = true;
    const read = () => client.subagentTail(target.roomId, target.agentId, target.subagentId)
      .then((t) => { if (live) setTail(t); }).catch(() => {});
    void read();
    const id = setInterval(read, 2_000);
    return () => { live = false; clearInterval(id); };
    // `key` is the target's identity
  }, [client, key]);
  return { tail };
}
