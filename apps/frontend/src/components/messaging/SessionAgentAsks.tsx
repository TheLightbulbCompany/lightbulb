"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, type AgentAskReceipt, type MessagingClient } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";

export type AgentAskClient = Pick<MessagingClient, "listAgentAsks" | "decideAgentAsk">;

// ponytail: polls; a live event for new asks if 10 s ever feels slow.
const POLL_MS = 10_000;

/**
 * Agents' asks to message this session, above the terminal view (which drops mouse clicks, so
 * the terminal band's own buttons cannot be clicked here). Always allow = send this one and flip
 * the account switch Settings -> Desktop shows ("Agents can message my sessions without asking").
 */
export function SessionAgentAsks({ client, sessionId, title }: { client: AgentAskClient; sessionId: string; title: string }) {
  const api = useApi();
  const [asks, setAsks] = useState<AgentAskReceipt[]>([]);
  const [settled, setSettled] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(() => {
    client.listAgentAsks(sessionId)
      .then(next => setAsks(next.filter(a => a.state === "awaiting_approval")))
      .catch(() => {}); // an older backend, or the ask list is not ours to read: no strip
  }, [client, sessionId]);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  async function answer(ask: AgentAskReceipt, decision: "send" | "decline", always = false) {
    setBusy(ask.requestId);
    try {
      await client.decideAgentAsk(sessionId, ask.requestId, decision);
      if (always) await api.put("/users/me/prefs", { agentSendWithoutAsking: true });
      setAsks(list => list.filter(a => a.requestId !== ask.requestId));
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) setSettled(s => ({ ...s, [ask.requestId]: "Already answered" }));
      else setSettled(s => ({ ...s, [ask.requestId]: "Could not answer. Try again." }));
    } finally {
      setBusy(null);
    }
  }

  if (asks.length === 0) return null;
  return <div className="flex flex-col gap-2" role="region" aria-label="Agent requests">
    {asks.map(ask => {
      const note = settled[ask.requestId];
      const done = note === "Already answered";
      return <div key={ask.requestId} className="rounded-md border p-3 text-ui">
        <p className="font-medium">{ask.agentName ?? "Your agent"} wants to {ask.kind === "interrupt" ? "stop" : "message"} {title}</p>
        {ask.text && <p className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">{ask.text}</p>}
        {note && <p className="mt-2 text-muted-foreground" role="status">{note}</p>}
        {!done && <div className="mt-2 flex flex-wrap gap-2">
          <Button size="sm" disabled={busy === ask.requestId} onClick={() => answer(ask, "send")}>Send</Button>
          <Button size="sm" variant="outline" disabled={busy === ask.requestId} onClick={() => answer(ask, "decline")}>Decline</Button>
          <Button size="sm" variant="outline" disabled={busy === ask.requestId} onClick={() => answer(ask, "send", true)}>Always allow</Button>
        </div>}
      </div>;
    })}
  </div>;
}
