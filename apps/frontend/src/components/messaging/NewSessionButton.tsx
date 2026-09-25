"use client";

import { useEffect, useId, useState } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { SquareTerminal } from "lucide-react";
import type { LiveEvent, LocalSession } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { useApi } from "@/lib/api";

type Option = { id: string; label: string };
type Tool = { id: "claude" | "codex"; label: string; modeLabel: string; models: Option[]; modes: Option[] };
type Mac = { deviceId: string; generation: string; name: string };
type MacOptions = { sharing: boolean; folders: string[]; tools: Tool[] };
type Pending = { startId: string; tool: string; known: Set<string>; phase: "approve" | "starting" | "failed"; error?: string };

/**
 * Start a Claude Code / Codex session on the caller's OWN paired Mac. Everything here
 * is a choice among what that Mac reported (installed CLIs, approved folders); the Mac
 * re-validates, asks natively, and opens Terminal. The new session opens once it
 * reports in under Sessions.
 */
export function NewSessionButton({ sessions, onEvent, onOpenSession }: {
  sessions: readonly LocalSession[];
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  onOpenSession: (sessionId: string) => void;
}) {
  const api = useApi();
  const { userId } = useAuth();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [deviceId, setDeviceId] = useState("");
  const [tool, setTool] = useState("");
  const [folder, setFolder] = useState("");
  const [model, setModel] = useState("");
  const [mode, setMode] = useState("");
  const [prompt, setPrompt] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);

  // Hidden unless desktop execution is on for this workspace; refreshed on open.
  const { data: targets, mutate: refreshTargets } = useSWR("desktop-session-targets",
    () => api.get("/desktop-sessions/targets") as Promise<{ enabled: boolean; devices: Mac[] }>);
  const macs = targets?.enabled ? targets.devices : null;
  const mac = macs?.find((m) => m.deviceId === deviceId) ?? macs?.[0];
  const { data: options, error: optionsError } = useSWR(open && mac ? ["desktop-session-options", mac.deviceId, mac.generation] : null,
    () => api.get(`/desktop-sessions/targets/${encodeURIComponent(mac!.deviceId)}/options?generation=${encodeURIComponent(mac!.generation)}`) as Promise<MacOptions>,
    { revalidateOnFocus: false });
  const toolId = tool || options?.tools[0]?.id || "";
  const folderPath = folder || options?.folders[0] || "";

  // The Mac's answer arrives as a live event; the session itself arrives in the list.
  useEffect(() => {
    if (!pending) return;
    return onEvent((event) => {
      if (event.type !== "runtime.operation.changed" || event.payload.operationId !== pending.startId) return;
      setPending((p) => p && p.startId === event.payload.operationId
        ? event.payload.state === "failed"
          ? { ...p, phase: "failed", error: event.payload.error?.message ?? "The Mac did not start the session." }
          : event.payload.state === "succeeded" ? { ...p, phase: "starting" } : p
        : p);
    });
  }, [pending, onEvent]);
  useEffect(() => {
    if (!pending || pending.phase === "failed") return;
    const fresh = sessions.find((s) => s.humanUserId === userId && s.provider === pending.tool && !pending.known.has(s.sessionId));
    if (!fresh) return;
    // Reacting to the external session list: the new session has reported in.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPending(null); setOpen(false); onOpenSession(fresh.sessionId);
  }, [sessions, pending, userId, onOpenSession]);

  if (!macs) return null;
  const selected = options?.tools.find((t) => t.id === toolId);
  const busy = pending !== null && pending.phase !== "failed";
  const canStart = !!mac && !!options?.sharing && !!selected && !!folderPath && !busy;

  async function start() {
    if (!canStart || !mac) return;
    const known = new Set(sessions.map((s) => s.sessionId));
    setPending({ startId: "", tool: toolId, known, phase: "approve" });
    try {
      const { startId } = await api.post("/desktop-sessions/start", {
        deviceId: mac.deviceId, generation: mac.generation, tool: toolId, folder: folderPath,
        model: model || null, mode: mode || null, prompt,
      }) as { startId: string };
      setPending((p) => p && { ...p, startId });
    } catch (error) {
      setPending({ startId: "", tool: toolId, known, phase: "failed", error: error instanceof Error ? error.message : "Could not start the session." });
    }
  }

  return <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (next) void refreshTargets(); }}>
    <DialogTrigger asChild><Button variant="ghost" size="icon-sm" aria-label="New session"><SquareTerminal aria-hidden="true" /></Button></DialogTrigger>
    <DialogContent className="room-creation-dialog">
      <form aria-label="New session" className="room-creation" onSubmit={(e) => { e.preventDefault(); void start(); }}>
        <div className="room-creation-header">
          <DialogTitle>New session</DialogTitle>
          <DialogDescription>Start Claude Code or Codex in Terminal on your Mac. It is shared with your workspace.</DialogDescription>
        </div>
        <div className="room-creation-body">
          {macs.length === 0 && <p role="status">No Mac of yours is ready. Open the latest Lightbulb desktop app on your Mac.</p>}
          {macs.length > 1 && <label htmlFor={`${id}-mac`}>Mac</label>}
          {macs.length > 1 && <select id={`${id}-mac`} className="room-input" value={mac?.deviceId} disabled={busy} onChange={(e) => { setDeviceId(e.target.value); setTool(""); setFolder(""); setModel(""); setMode(""); }}>
            {macs.map((m) => <option key={m.deviceId} value={m.deviceId}>{m.name}</option>)}
          </select>}
          {mac && !options && !optionsError && <p role="status">Checking {mac.name}…</p>}
          {optionsError && <p role="alert" className="text-err">Could not reach that Mac. Is the Lightbulb app open on it?</p>}
          {options && !options.sharing && <p role="alert">Session sharing is off on this Mac. Turn it on in the desktop app’s Settings first.</p>}
          {options && options.tools.length === 0 && <p role="status">Neither Claude Code nor Codex is installed on this Mac.</p>}
          {options && options.folders.length === 0 && <p role="status">This Mac has no approved folders. Add one in the desktop app’s Settings → File access.</p>}
          {options && selected && <>
            <label htmlFor={`${id}-tool`}>Tool</label>
            <select id={`${id}-tool`} className="room-input" value={toolId} disabled={busy} onChange={(e) => { setTool(e.target.value); setModel(""); setMode(""); }}>
              {options.tools.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
            <label htmlFor={`${id}-folder`}>Folder</label>
            <select id={`${id}-folder`} className="room-input" value={folderPath} disabled={busy} onChange={(e) => setFolder(e.target.value)}>
              {options.folders.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <label htmlFor={`${id}-model`}>Model</label>
            <select id={`${id}-model`} className="room-input" value={model} disabled={busy} onChange={(e) => setModel(e.target.value)}>
              <option value="">Default</option>
              {selected.models.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </select>
            <label htmlFor={`${id}-mode`}>{selected.modeLabel}</label>
            <select id={`${id}-mode`} className="room-input" value={mode} disabled={busy} onChange={(e) => setMode(e.target.value)}>
              <option value="">Default</option>
              {selected.modes.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </select>
            <label htmlFor={`${id}-prompt`}>First message</label>
            <textarea id={`${id}-prompt`} className="room-input" rows={4} maxLength={4000} value={prompt} disabled={busy} onChange={(e) => setPrompt(e.target.value)} />
          </>}
          {pending?.phase === "approve" && <p role="status">Approve the start on your Mac.</p>}
          {pending?.phase === "starting" && <p role="status">Opening in Terminal. It opens here once it appears under Sessions.</p>}
          {pending?.phase === "failed" && <p role="alert" className="text-err">{pending.error}</p>}
        </div>
        <DialogFooter className="px-6 pb-6">
          <Button type="submit" disabled={!canStart}>Start session</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>;
}
