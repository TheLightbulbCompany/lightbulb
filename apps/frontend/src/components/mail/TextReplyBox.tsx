"use client";

import { useState } from "react";
import { useSWRConfig } from "swr";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { isTextLineKey } from "./useTeamMail";

/** Reply to a customer text as a person, and take the thread over from the agent (or hand it back). */
export function TextReplyBox({ messageKey, humanThread }: { messageKey: string; humanThread: boolean }) {
  const api = useApi();
  const { mutate } = useSWRConfig();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  const run = async (call: () => Promise<{ status: string }>, onOk: () => void) => {
    setBusy(true);
    setStatus(null);
    try {
      const { status: s } = await call();
      if (s === "sent" || s === "ok") onOk();
      else setStatus(s);
    } catch {
      setStatus("send_failed");
    } finally {
      setBusy(false);
    }
  };

  const send = () => run(
    () => api.post("/agent-text/reply", { key: messageKey, text }) as Promise<{ status: string }>,
    () => { setText(""); void mutate(isTextLineKey); },
  );
  const toggle = () => run(
    () => api.post("/agent-text/thread", { key: messageKey, mode: humanThread ? "agent" : "human" }) as Promise<{ status: string }>,
    () => void mutate(isTextLineKey),
  );

  return <div className="mail-reply">
    <textarea className="mail-reply__input" aria-label="Reply by text" rows={3} value={text}
      onChange={(e) => setText(e.target.value)} />
    <div className="mail-reply__actions">
      <Button type="button" size="sm" disabled={busy || !text.trim()} onClick={() => void send()}>Send</Button>
      <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void toggle()}>
        {humanThread ? "Hand back" : "Take over"}
      </Button>
      {status && <span role="status" className="mail-reply__status">{status}</span>}
    </div>
  </div>;
}
