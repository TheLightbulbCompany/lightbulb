"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, type AgentAskReceipt, type LiveEvent, type MessagingClient } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";

export type AgentAskClient = Pick<MessagingClient, "listAgentAsks" | "decideAgentAsk">;

/**
 * Agents' asks to message this session, above the terminal view (which drops mouse clicks, so
 * the terminal band's own buttons cannot be clicked here). Always allow = send this one and flip
 * the account switch Settings -> Desktop shows ("Agents can message my sessions without asking").
 */
export function SessionAgentAsks({ client, sessionId, title, onEvent, connected = true }: {
  client: AgentAskClient; sessionId: string; title: string;
  /** Live events: an ask filed for this session (or any session change) refetches. No timer. */
  onEvent?: (handler: (event: LiveEvent) => void) => () => void;
  /** Flips to true on a (re)connect, which refetches once to catch what was missed. */
  connected?: boolean;
}) {
  const api = useApi();
  const [asks, setAsks] = useState<AgentAskReceipt[]>([]);
  const [settled, setSettled] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(() => {
    client.listAgentAsks(sessionId)
      .then(next => setAsks(next.filter(a => a.state === "awaiting_approval")))
      .catch(error => {
        // Not ours to read (403/404): no strip. Anything else keeps what is shown.
        if (error instanceof ApiError && (error.status === 403 || error.status === 404)) setAsks([]);
      });
  }, [client, sessionId]);
  useEffect(() => { if (connected) refresh(); }, [refresh, connected]);
  useEffect(() => onEvent?.(event => {
    if (event.type === "workspace.sessions.changed" || (event.type === "workspace.session.attention" && event.payload.sessionId === sessionId)) refresh();
  }), [onEvent, refresh, sessionId]);

  async function answer(ask: AgentAskReceipt, decision: "send" | "decline", always = false) {
    setBusy(ask.requestId);
    let saved = !always;
    try {
      // The setting first, as the Activity action does: a retry after a failed send still has it.
      if (always) { await api.put("/users/me/prefs", { agentSendWithoutAsking: true }); saved = true; }
      await client.decideAgentAsk(sessionId, ask.requestId, decision);
      setAsks(list => list.filter(a => a.requestId !== ask.requestId));
    } catch (error) {
      const note = !saved ? "Could not save the setting. Nothing was sent."
        : error instanceof ApiError && error.status === 409 ? "Already answered"
        : always ? "Setting saved, but this request was not sent. Try again." : "Could not answer. Try again.";
      setSettled(s => ({ ...s, [ask.requestId]: note }));
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
