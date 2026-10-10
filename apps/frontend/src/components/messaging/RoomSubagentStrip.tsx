"use client";
import { useEffect, useState } from "react";
import { isSubagentGone, liveSubagents, sessionClock, subagentActivity, useSubagentTail, type MessagingClient, type RoomSubagents, type SubagentProgress } from "@lightbulb/shared";

const PREVIEW = 3; // upstream Desktop previews three workers
const GONE = "That subagent has already finished.";
const ENDED: Record<string, string> = { completed: "Finished", failed: "Failed", interrupted: "Stopped" };

function useNow(on: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, [on]);
  return now;
}

function Detail({ item, roomId, client, onSteer, onStop }: {
  item: SubagentProgress; roomId: string; client: MessagingClient;
  onSteer: (agentId: string, id: string, text: string) => Promise<unknown>;
  onStop: (agentId: string, id: string) => Promise<unknown>;
}) {
  const live = item.status === "running" || item.status === "queued";
  // A finished child stops polling but keeps its last transcript on screen until the person closes it.
  const { tail } = useSubagentTail(client, live ? { roomId, agentId: item.agentId, subagentId: item.id } : null);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const run = (call: () => Promise<unknown>, ok: string, after?: () => void) => {
    if (busy) return;
    setBusy(true);
    call().then(() => { after?.(); setNote(ok); },
      (e) => setNote(isSubagentGone(e) ? GONE : "Could not reach the agent. Try again."))
      .finally(() => setBusy(false));
  };
  const off = busy || !live;
  return <div className="room-subagent-detail">
    {!live && <p className="room-subagent-note" role="status">{ENDED[item.status] ?? "Finished"}</p>}
    <form className="room-subagent-steer" onSubmit={(e) => {
      e.preventDefault();
      const value = text.trim();
      if (!value) return;
      run(() => onSteer(item.agentId, item.id, value), "Steer queued", () => setText(""));
    }}>
      <input aria-label={`Steer ${item.goal}`} value={text} onChange={(e) => setText(e.target.value)} placeholder="Steer this subagent" disabled={off} />
      <button type="submit" disabled={off || !text.trim()}>Steer</button>
      <button type="button" disabled={off} onClick={() => run(() => onStop(item.agentId, item.id), "Stopping…")}>Stop</button>
    </form>
    {note && <p className="room-subagent-note" role="status">{note}</p>}
    <pre className="room-subagent-tail">{tail?.available ? tail.text : (!live && item.summary) || "No transcript yet."}</pre>
  </div>;
}

export function RoomSubagentStrip({ state, roomId, agentName, client, onSteer, onStop, onVisible }: {
  state: RoomSubagents; roomId: string; agentName: (agentId: string) => string; client: MessagingClient;
  onSteer: (agentId: string, id: string, text: string) => Promise<unknown>;
  onStop: (agentId: string, id: string) => Promise<unknown>;
  onVisible: (visible: boolean) => void;
}) {
  const live = liveSubagents(state);
  const [open, setOpen] = useState(true);
  const [all, setAll] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const now = useNow(live.length > 0);
  const visible = live.length > 0 && open;
  useEffect(() => { onVisible(visible); return () => onVisible(false); }, [visible, onVisible]);
  // From all items, not the live list: the pane stays open on a child that just finished.
  const pickedItem = state.items.find((i) => i.id === picked);
  if (!live.length && !pickedItem) return null;
  const rows = all ? live : live.slice(0, PREVIEW);
  return <div className="room-subagent-strip" role="region" aria-label="Running subagents">
    <button type="button" className="room-subagent-heading" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
      Subagents ({live.length})
    </button>
    {open && <>
      {rows.map((item) => <button key={item.id} type="button" className="room-subagent-row" aria-expanded={picked === item.id}
        onClick={() => setPicked((p) => (p === item.id ? null : item.id))}>
        <span className="room-subagent-spinner" aria-hidden="true" />
        <span className="room-subagent-goal">{agentName(item.agentId)} · {item.goal}</span>
        <span className="room-subagent-activity">{subagentActivity(item)}</span>
        <span className="room-subagent-meta">{sessionClock(now - item.startedAt, true)}</span>
      </button>)}
      {!all && live.length > PREVIEW && <button type="button" className="room-subagent-row room-subagent-more" onClick={() => setAll(true)}>+{live.length - PREVIEW} more</button>}
      {pickedItem && <Detail item={pickedItem} roomId={roomId} client={client} onSteer={onSteer} onStop={onStop} />}
    </>}
  </div>;
}
