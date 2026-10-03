"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { Ellipsis, SquareTerminal, X } from "lucide-react";
import { ApiError, type LiveEvent, type LocalSession } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useApi } from "@/lib/api";
import { SessionToolIcon } from "./SessionToolIcon";
import { startLocalTerminal, useLocalMac } from "./localTerminal";

type Option = { id: string; label: string };
type Tool = { id: "claude" | "codex"; label: string; modeLabel: string; models: Option[]; modes: Option[]; efforts?: Option[]; defaultMode?: string; defaultEffort?: string | null };
type Mac = { deviceId: string; generation: string; name: string };
type MacOptions = { name?: string | null; sharing: boolean; folders: string[]; tools: Tool[] };
type Targets = { enabled: boolean; devices: Mac[] };
const MAX_PROMPT_BYTES = 4096; // the backend's byte cap
const PENDING_MS = 150_000; // longer than the backend's wait for the Mac
type Pending = { startId: string; scope: string; startedAt: number; phase: "starting" | "opened" | "unknown" | "failed"; error?: string };

const TARGETS_KEY = "desktop-session-targets";
const pendingKey = (scope: string) => `lightbulb.newSession.pending.${scope}`;
function readPending(scope: string): Pending | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(pendingKey(scope)) ?? "null");
    if (!value || typeof value !== "object") return null;
    const item = value as Partial<Pending>;
    return item.scope === scope && typeof item.startId === "string" && typeof item.startedAt === "number"
      && Date.now() >= item.startedAt && Date.now() - item.startedAt < 6 * 60 * 60 * 1000
      ? { startId: item.startId, scope, startedAt: item.startedAt, phase: "unknown",
        error: "Could not confirm whether your Mac started this session. Check the Lightbulb app and Sessions before trying again." } : null;
  } catch { return null; }
}
/** The viewer's own paired Macs (id, generation, name). `enabled` false: no request. */
export function useTargets(enabled = true) {
  const api = useApi();
  return useSWR(enabled ? TARGETS_KEY : null, () => api.get("/desktop-sessions/targets") as Promise<Targets>);
}

// Last-used Mac, tool and folders (most recent first): a per-viewer convenience on this device.
type LastUsed = { deviceId?: string; tool?: string; folders?: string[] };
const lastUsedKey = (userId: string | null | undefined) => `lightbulb.newSession.${userId ?? "anon"}`;
function readLastUsed(userId: string | null | undefined): LastUsed {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(lastUsedKey(userId)) ?? "{}");
    if (!parsed || typeof parsed !== "object") return {};
    const { deviceId, tool, folders } = parsed as Record<string, unknown>;
    return {
      deviceId: typeof deviceId === "string" ? deviceId : undefined,
      tool: typeof tool === "string" ? tool : undefined,
      folders: Array.isArray(folders) ? folders.filter((f): f is string => typeof f === "string") : [],
    };
  } catch { return {}; }
}
function writeLastUsed(userId: string | null | undefined, last: LastUsed) {
  try { localStorage.setItem(lastUsedKey(userId), JSON.stringify(last)); } catch { /* storage off: nothing to remember */ }
}
// Desktop builds before the self-start fix could not record an approval at all, so
// their DENIED always means "update the app", never a real refusal.
function failure(error: { code?: string; message?: string } | null | undefined) {
  if (error?.code === "DENIED") return "Your Lightbulb desktop app needs an update to start sessions. Update it on your Mac, then try again.";
  return error?.message ?? "The Mac did not start the session.";
}
const shortPath = (path: string) => path.replace(/^\/Users\/[^/]+(?=\/|$)/, "~");

/** The sidebar's New session button. Hidden unless desktop execution is on for this workspace. */
export function NewSessionButton({ onOpen }: { onOpen: () => void }) {
  const { data: targets } = useTargets();
  const localMac = useLocalMac();
  if (!targets?.enabled && !localMac) return null;
  return <Button variant="ghost" size="icon-sm" aria-label="New session" onClick={onOpen}><SquareTerminal aria-hidden="true" /></Button>;
}

/**
 * An empty session you just type into, the way Claude Code and Codex start one.
 * Every choice is among what the caller's OWN Mac reported (installed CLIs, approved
 * folders), pre-filled from last use; the Mac re-validates and opens the session there (no
 * prompt there: only the person the Mac is paired to can start sessions on it). The view becomes the live session once it reports in under Sessions.
 */
export function NewSessionView({ sessions, onEvent, onOpenSession, onClose }: {
  sessions: readonly LocalSession[];
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  onOpenSession: (sessionId: string) => void;
  onClose: () => void;
}) {
  const api = useApi();
  const { userId, orgId } = useAuth();
  const scope = userId ? `${userId}:${orgId ?? userId}` : null;
  const [last] = useState(() => readLastUsed(userId));
  const [deviceId, setDeviceId] = useState(last.deviceId ?? "");
  const [tool, setTool] = useState(last.tool ?? "");
  const [folder, setFolder] = useState("");
  const [model, setModel] = useState("");
  const [mode, setMode] = useState("");
  const [effort, setEffort] = useState("");
  const [prompt, setPrompt] = useState("");
  const [pendingState, setPending] = useState<Pending | null>(() => scope ? readPending(scope) : null);
  const pending = pendingState?.scope === scope ? pendingState : null;
  useEffect(() => {
    if (!scope || !pending) return;
    try {
      if (pending.phase === "failed") localStorage.removeItem(pendingKey(scope));
      else localStorage.setItem(pendingKey(scope), JSON.stringify(pending));
    } catch { /* Browser storage may be disabled; the current view still holds the start. */ }
  }, [scope, pending]);

  // Inside the desktop app this Mac answers for itself and the session opens in its own window.
  const localMac = useLocalMac();
  const { data: targets, mutate: refreshTargets } = useTargets();
  useEffect(() => { void refreshTargets(); }, [refreshTargets]);
  const macs = !localMac && targets?.enabled ? targets.devices : null;
  const mac = macs?.find((m) => m.deviceId === deviceId) ?? macs?.[0];
  const { data: remoteOptions, error: optionsError } = useSWR(!localMac && mac ? ["desktop-session-options", mac.deviceId, mac.generation] : null,
    () => api.get(`/desktop-sessions/targets/${encodeURIComponent(mac!.deviceId)}/options?generation=${encodeURIComponent(mac!.generation)}`) as Promise<MacOptions>,
    { revalidateOnFocus: false });
  const options: MacOptions | undefined = localMac ?? remoteOptions;
  // The Mac's own name (it reports it); the target list only knows "Your Mac".
  const macName = options?.name || mac?.name || "your Mac";
  const selected = options?.tools.find((t) => t.id === tool) ?? options?.tools[0];
  const recent = last.folders ?? [];
  const rank = (f: string) => { const i = recent.indexOf(f); return i < 0 ? recent.length : i; };
  const folders = options ? [...options.folders].sort((a, b) => rank(a) - rank(b)) : [];
  const folderPath = folders.includes(folder) ? folder : folders[0] ?? "";
  const modelId = selected?.models.some((o) => o.id === model) ? model : "";
  const modeId = selected?.modes.some((o) => o.id === mode) ? mode : selected?.defaultMode ?? "";
  const effortId = selected?.efforts?.some((o) => o.id === effort) ? effort : selected?.defaultEffort ?? "";
  const modeChoice = selected?.modes.some((o) => o.id === mode) ? mode : "";
  const effortChoice = selected?.efforts?.some((o) => o.id === effort) ? effort : "";

  // A lost event, a restarted backend task or a session that never reports in must not
  // leave the view stuck.
  useEffect(() => {
    if (!pending || pending.phase === "failed" || pending.phase === "unknown") return;
    const timer = setTimeout(() => setPending((p) => p && p.startId === pending.startId
      ? { ...p, phase: "unknown", error: p.phase === "opened"
        ? "The session opened on your Mac, but it never reported in. Check the Lightbulb app there."
        : "Could not confirm whether your Mac started this session. Check the Lightbulb app and Sessions before trying again." } : p), PENDING_MS);
    return () => clearTimeout(timer);
  }, [pending]);

  // The Mac's answer arrives as a live event; the session itself arrives in the list.
  useEffect(() => {
    if (!pending) return;
    return onEvent((event) => {
      if (event.type !== "runtime.operation.changed" || event.payload.operationId !== pending.startId) return;
      setPending((p) => p && p.startId === event.payload.operationId
        ? event.payload.state === "failed"
          ? event.payload.error?.retryable === false
            || ["desktop_generation_lost", "desktop_busy", "desktop_forward_loop"].includes(event.payload.error?.code ?? "")
            ? { ...p, phase: "failed", error: failure(event.payload.error) }
            : { ...p, phase: "unknown", error: "Could not confirm whether your Mac started this session. Check the Lightbulb app and Sessions before trying again." }
          : event.payload.state === "succeeded" ? { ...p, phase: "opened" } : p
        : p);
    });
  }, [pending, onEvent]);
  useEffect(() => {
    if (!pending || pending.phase === "failed") return;
    const fresh = sessions.find((s) => s.humanUserId === userId && s.startId === pending.startId);
    // The new session has reported in: this view becomes it.
    if (fresh) {
      try { if (scope) localStorage.removeItem(pendingKey(scope)); } catch { /* storage off */ }
      setPending(null);
      onOpenSession(fresh.sessionId);
    }
  }, [sessions, pending, userId, scope, onOpenSession]);

  const busy = pending !== null && pending.phase !== "failed";
  const tooLong = new TextEncoder().encode(prompt).length > MAX_PROMPT_BYTES;
  const blocked = localMac
    ? (localMac.tools.length === 0 ? `Install Claude Code or Codex on ${macName} first.`
      : folders.length === 0 ? "Approve a folder in the desktop app first: Settings → File access." : null)
    : !targets ? null
    : !macs ? "Starting sessions from Lightbulb is not on for this workspace."
    : !mac ? "No Mac is ready. Open the Lightbulb desktop app on your Mac and sign in."
    : optionsError ? `Can't reach ${macName}. Is the Lightbulb app open on it?`
    : !options ? null
    : !options.sharing ? "sharing"
    : options.tools.some((t) => !t.defaultMode || !Array.isArray(t.efforts)) ? `Update Lightbulb on ${macName} to choose safe session settings.`
    : options.tools.length === 0 ? `Install Claude Code or Codex on ${macName} first.`
    : folders.length === 0 ? "Approve a folder in the desktop app first: Settings → File access."
    : null;
  const ready = !!options && !!selected && !blocked;
  const canStart = !!scope && (!!localMac || (!!mac && !!options?.sharing)) && !!selected && !!folderPath && !blocked && !busy && !tooLong && !!prompt.trim();

  async function start() {
    if (!canStart || !scope || !selected || (!localMac && !mac)) return;
    writeLastUsed(userId, { deviceId: mac?.deviceId, tool: selected.id, folders: [folderPath, ...recent.filter((f) => f !== folderPath)].slice(0, 20) });
    // Minted here so the Mac's answer can never arrive before we know what to listen for.
    const startId = crypto.randomUUID();
    const start = { startId, scope, startedAt: Date.now() };
    setPending({ ...start, phase: "starting" });
    if (localMac) {
      try {
        const { linked } = await startLocalTerminal({ startId, tool: selected.id, folder: folderPath,
          model: modelId || null, mode: modeId || null, effort: effortId || null, prompt });
        // Linked: wait here until it reports in, then become it. Not linked (sharing off): it
        // lives only in its window.
        if (linked) setPending({ ...start, phase: "opened" });
        else { setPending(null); onClose(); }
      } catch (error) {
        setPending({ ...start, phase: "failed", error: error instanceof Error ? error.message : String(error) });
      }
      return;
    }
    if (!mac) return;
    try {
      await api.post("/desktop-sessions/start?source=web", {
        startId, deviceId: mac.deviceId, generation: mac.generation, tool: selected.id, folder: folderPath,
        model: modelId || null, mode: modeId || null, effort: effortId || null, prompt,
      });
    } catch (error) {
      setPending(error instanceof ApiError && error.status < 500
        ? { ...start, phase: "failed", error: error.message }
        : { ...start, phase: "unknown", error: "Could not confirm whether your Mac started this session. Check the Lightbulb app and Sessions before trying again." });
    }
  }

  const where = mac || localMac ? `${macName}${folderPath ? ` · ${shortPath(folderPath)}` : ""}` : "";
  return <section className="room-panel room-session-focus" aria-label="New session">
    <div className="room-session-main">
      <header className="room-context-header">
        <h3 className="font-semibold">New session</h3>
        <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label="Close new session" onClick={onClose}><X aria-hidden="true" /></Button>
      </header>
      <div className="new-session-body text-sm text-muted-foreground">
        {blocked === "sharing"
          ? <p role="status">Session sharing is off on {macName}. Turn it on in <Link className="underline underline-offset-4" href="/settings">Settings → Native terminal sessions</Link>.</p>
          : blocked && <p role="status">{blocked}</p>}
        {pending?.phase === "unknown" && <Button variant="link" size="sm" className="h-auto p-0"
          onClick={() => { try { if (scope) localStorage.removeItem(pendingKey(scope)); } catch { /* storage off */ } setPending(null); }}>
          Dismiss and start another</Button>}
        {busy && <p role="status">{pending?.phase === "unknown" ? pending.error
          : pending?.phase === "opened" ? (localMac ? "Opened in its own window. Waiting for the session…" : `Opened on ${where}. Waiting for the session…`) : `Starting on ${where}…`}</p>}
        {pending?.phase === "failed" && <p role="alert" className="text-err">{pending.error}</p>}
        {tooLong && <p role="alert" className="text-err">The first message is too long.</p>}
      </div>
      {(ready || (macs && macs.length > 1)) && <div className="new-session-strip">
        {/* The Mac chip stays even when this Mac is blocked: another one may not be. */}
        {macs && macs.length > 1 && <select aria-label="Mac" className="new-session-chip" value={mac?.deviceId} disabled={busy}
          onChange={(e) => { setDeviceId(e.target.value); setFolder(""); }}>
          {macs.map((m) => <option key={m.deviceId} value={m.deviceId}>{m.name}</option>)}
        </select>}
        {ready && selected && <>
        <div role="group" aria-label="Tool" className="new-session-toggle">
          {options.tools.map((t) => <button key={t.id} type="button" aria-pressed={t.id === selected.id} disabled={busy} onClick={() => setTool(t.id)}>
            <span aria-hidden="true"><SessionToolIcon provider={t.id} className="size-3.5" /></span>{t.label}
          </button>)}
        </div>
        <select aria-label="Folder" className="new-session-chip" value={folderPath} title={folderPath} disabled={busy} onChange={(e) => setFolder(e.target.value)}>
          {folders.map((f) => <option key={f} value={f}>{shortPath(f)}</option>)}
        </select>
        <Popover>
          <PopoverTrigger asChild><Button variant="ghost" size="icon-sm" aria-label="Session settings" disabled={busy}><Ellipsis aria-hidden="true" /></Button></PopoverTrigger>
          <PopoverContent align="start" className="grid w-64 gap-2 text-sm">
            <label className="grid gap-1">Model
              <select className="room-input" value={modelId} onChange={(e) => setModel(e.target.value)}>
                <option value="">Default</option>
                {selected.models.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
            </label>
            <label className="grid gap-1">{selected.modeLabel}
              <select className="room-input" value={modeChoice} onChange={(e) => setMode(e.target.value)}>
                <option value="">Default</option>
                {selected.modes.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
            </label>
            <label className="grid gap-1">Reasoning effort
              <select className="room-input" value={effortChoice} onChange={(e) => setEffort(e.target.value)}>
                <option value="">Default</option>
                {selected.efforts?.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
            </label>
          </PopoverContent>
        </Popover>
        </>}
      </div>}
      <form className="room-composer" aria-label="New session composer" onSubmit={(e) => { e.preventDefault(); void start(); }}>
        <label className="sr-only" htmlFor="new-session-prompt">First message</label>
        <textarea id="new-session-prompt" rows={2} autoFocus placeholder="What do you want to do?" value={prompt}
          disabled={busy || (!!blocked)} onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />
      </form>
    </div>
  </section>;
}
