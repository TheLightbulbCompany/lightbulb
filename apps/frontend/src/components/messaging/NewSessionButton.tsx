"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { SquareTerminal, X } from "lucide-react";
import { ApiError, type LiveEvent, type LocalSession } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useApi } from "@/lib/api";
import { SessionToolIcon } from "./SessionToolIcon";
import { startLocalTerminal, useLocalMac, useLocalTerminal } from "./localTerminal";

type Option = { id: string; label: string };
type Tool = { id: "claude" | "codex"; label: string; modeLabel: string; models: Option[]; modes: Option[]; efforts?: Option[]; defaultMode?: string; defaultEffort?: string | null };
type Mac = { deviceId: string; generation: string; name: string };
type MacOptions = { name?: string | null; sharing: boolean; folders: string[]; tools: Tool[] };
type Targets = { enabled: boolean; devices: Mac[] };
const PENDING_MS = 150_000; // longer than the backend's wait for the Mac
const HOLDER_GONE_MS = 10_000; // five of the terminal probe's 2 s re-asks
type Pending = { startId: string; scope: string; startedAt: number; phase: "starting" | "opened" | "unknown" | "failed"; error?: string; holderId?: string; tool?: Tool["id"] };

const TARGETS_KEY = "desktop-session-targets";
const pendingKey = (scope: string) => `lightbulb.newSession.pending.${scope}`;
function readPending(scope: string): Pending | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(pendingKey(scope)) ?? "null");
    if (!value || typeof value !== "object") return null;
    const item = value as Partial<Pending>;
    if (item.scope !== scope || typeof item.startId !== "string" || typeof item.startedAt !== "number"
      || Date.now() < item.startedAt) return null;
    const base = { startId: item.startId, scope, startedAt: item.startedAt };
    // A start this Mac holds: the Mac itself says whether it still runs (the holder probe), at any age.
    if (item.phase === "opened" && typeof item.holderId === "string")
      return { ...base, phase: "opened", holderId: item.holderId, tool: item.tool === "claude" || item.tool === "codex" ? item.tool : undefined };
    if (Date.now() - item.startedAt >= 6 * 60 * 60 * 1000) return null;
    return { ...base, phase: "unknown",
      error: "Could not confirm whether your Mac started this session. Check the Lightbulb app and Sessions before trying again." };
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
  const [tool, setTool] = useState(last.tool ?? ""); // preselects the last-used tool
  const [folder, setFolder] = useState("");
  const [model, setModel] = useState("");
  const [mode, setMode] = useState("");
  const [effort, setEffort] = useState("");
  const [pendingState, setPending] = useState<Pending | null>(() => scope ? readPending(scope) : null);
  const pending = pendingState?.scope === scope ? pendingState : null;
  useEffect(() => {
    if (!scope || !pending) return;
    try {
      if (pending.phase === "failed") localStorage.removeItem(pendingKey(scope));
      else localStorage.setItem(pendingKey(scope), JSON.stringify(pending));
    } catch { /* Browser storage may be disabled; the current view still holds the start. */ }
  }, [scope, pending]);

  // Inside the desktop app this Mac answers for itself and the session opens in this panel.
  const localMac = useLocalMac();
  const slot = useRef<HTMLDivElement>(null);
  const local = useLocalTerminal(pending?.phase === "opened" && pending.holderId ? { holderId: pending.holderId } : null, slot, true);
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
  const modeChoice = selected?.modes.some((o) => o.id === mode) ? mode : "";
  const effortChoice = selected?.efforts?.some((o) => o.id === effort) ? effort : "";

  // A lost event, a restarted backend task or a session that never reports in must not
  // leave the view stuck.
  useEffect(() => {
    // A start this Mac holds has no deadline: Codex only reports in after its first prompt.
    if (!pending || pending.phase === "failed" || pending.phase === "unknown" || (pending.phase === "opened" && pending.holderId)) return;
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

  // The holder is gone before the session reported in (quit before a first prompt): nothing to show.
  // One missed probe is not proof (the hook keeps asking), so it must stay gone for HOLDER_GONE_MS.
  useEffect(() => {
    if (pending?.phase !== "opened" || !pending.holderId || local !== "none") return;
    const timer = setTimeout(() => {
      try { if (scope) localStorage.removeItem(pendingKey(scope)); } catch { /* storage off */ }
      setPending(null);
    }, HOLDER_GONE_MS);
    return () => clearTimeout(timer);
  }, [pending, local, scope]);

  const busy = pending !== null && pending.phase !== "failed";
  const blocked = localMac
    ? (localMac.tools.length === 0 ? `Install Claude Code or Codex on ${macName} first.`
      : folders.length === 0 ? "Approve a folder in the desktop app first: Settings → Desktop → Approved folders." : null)
    : !targets ? null
    : !macs ? "Starting sessions from Lightbulb is not on for this workspace."
    : !mac ? "No Mac is ready. Open the Lightbulb desktop app on your Mac and sign in."
    : optionsError ? `Can't reach ${macName}. Is the Lightbulb app open on it?`
    : !options ? null
    : !options.sharing ? "sharing"
    : options.tools.some((t) => !t.defaultMode || !Array.isArray(t.efforts)) ? `Update Lightbulb on ${macName} to choose safe session settings.`
    : options.tools.length === 0 ? `Install Claude Code or Codex on ${macName} first.`
    : folders.length === 0 ? "Approve a folder in the desktop app first: Settings → Desktop → Approved folders."
    : null;
  const ready = !!options && !!selected && !blocked;
  const canStart = !!scope && (!!localMac || (!!mac && !!options?.sharing)) && !!selected && !!folderPath && !blocked && !busy;

  async function start(chosen: Tool) {
    if (!canStart || !scope || (!localMac && !mac)) return;
    // Settings picked for one tool only apply if the tool you press offers them; else its defaults.
    const pick = (list: Option[] | undefined, v: string, dflt: string | null | undefined) => (list?.some((o) => o.id === v) ? v : dflt) || null;
    const choice = { model: pick(chosen.models, model, null), mode: pick(chosen.modes, mode, chosen.defaultMode), effort: pick(chosen.efforts, effort, chosen.defaultEffort) };
    writeLastUsed(userId, { deviceId: mac?.deviceId, tool: chosen.id, folders: [folderPath, ...recent.filter((f) => f !== folderPath)].slice(0, 20) });
    // Minted here so the Mac's answer can never arrive before we know what to listen for.
    const startId = crypto.randomUUID();
    const start = { startId, scope, startedAt: Date.now(), tool: chosen.id };
    setPending({ ...start, phase: "starting" });
    if (localMac) {
      try {
        const { holderId } = await startLocalTerminal({ startId, tool: chosen.id, folder: folderPath,
          ...choice, prompt: "" });
        // It shows here at once; once it reports in, this view becomes the session.
        setPending({ ...start, phase: "opened", holderId });
      } catch (error) {
        setPending({ ...start, phase: "failed", error: error instanceof Error ? error.message : String(error) });
      }
      return;
    }
    if (!mac) return;
    try {
      await api.post("/desktop-sessions/start?source=web", {
        startId, deviceId: mac.deviceId, generation: mac.generation, tool: chosen.id, folder: folderPath,
        ...choice, prompt: "",
      });
    } catch (error) {
      setPending(error instanceof ApiError && error.status < 500
        ? { ...start, phase: "failed", error: error.message }
        : { ...start, phase: "unknown", error: "Could not confirm whether your Mac started this session. Check the Lightbulb app and Sessions before trying again." });
    }
  }

  const where = mac || localMac ? `${macName}${folderPath ? ` · ${shortPath(folderPath)}` : ""}` : "";
  const dismiss = () => { try { if (scope) localStorage.removeItem(pendingKey(scope)); } catch { /* storage off */ } setPending(null); };
  const held = pending?.phase === "opened" && !!pending.holderId;
  return <section className="room-panel room-session-focus" aria-label="New session">
    <div className="room-session-main">
      <header className="room-context-header">
        <h3 className="font-semibold">New session</h3>
        <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label="Close new session" onClick={onClose}><X aria-hidden="true" /></Button>
      </header>
      {held ? <p role="status" className="new-session-status text-ui text-muted-foreground">{pending?.tool === "codex"
        ? "Running on this Mac. It joins Sessions after your first prompt. Quit Codex to start a different session."
        : "Running on this Mac. It moves to Sessions when it reports in."}</p>
        : busy && <div className="new-session-body text-ui text-muted-foreground">
          <p role="status">{pending?.phase === "unknown" ? pending.error
            : pending?.phase === "opened" ? `Opened on ${where}. Waiting for the session…` : `Starting on ${where}…`}</p>
          {pending?.phase === "unknown" && <Button variant="outline" size="sm" onClick={dismiss}>Dismiss and start another</Button>}
        </div>}
      {local !== "none" && <div ref={slot} className="local-session-terminal" role="region" aria-label="Session terminal" />}
    </div>
    <Dialog open={!busy} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent showCloseButton className="w-[calc(100%_-_2rem)] max-w-md max-h-[calc(100dvh_-_2rem)] gap-3 overflow-y-auto shadow-none">
        <DialogHeader>
          <DialogTitle>New session</DialogTitle>
          <DialogDescription>{`Opens a live terminal on ${macName} that your workspace can watch.`}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3 text-ui">
          {/* The Mac picker stays even when this Mac is blocked: another one may not be. */}
          {macs && macs.length > 1 && <label className="grid gap-1">Mac
            <select className="room-input" value={mac?.deviceId} onChange={(e) => { setDeviceId(e.target.value); setFolder(""); }}>
              {macs.map((m) => <option key={m.deviceId} value={m.deviceId}>{m.name}</option>)}
            </select>
          </label>}
          {blocked === "sharing"
            ? <p role="status" className="text-muted-foreground">Session sharing is off on {macName}. Turn it on in <Link className="underline underline-offset-4" href="/settings?panel=desktop">Settings → Desktop → Session sharing</Link>.</p>
            : blocked ? <p role="status" className="text-muted-foreground">{blocked}</p>
            : !ready && <p className="text-muted-foreground">Checking your Mac…</p>}
          {ready && selected && <>
            <div role="group" aria-label="Tool" className="new-session-tools">
              {options.tools.map((t) => <button key={t.id} type="button" aria-pressed={t.id === selected.id} onClick={() => setTool(t.id)}>
                <span aria-hidden="true"><SessionToolIcon provider={t.id} className="size-6" /></span>{t.label}
              </button>)}
            </div>
            <label className="grid gap-1">Folder
              <select className="room-input" value={folderPath} title={folderPath} onChange={(e) => setFolder(e.target.value)}>
                {folders.map((f) => <option key={f} value={f}>{shortPath(f)}</option>)}
              </select>
            </label>
            <details className="new-session-more">
              <summary>Model and mode</summary>
              <div className="grid gap-3 pt-2">
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
                {!!selected.efforts?.length && <label className="grid gap-1">Reasoning effort
                  <select className="room-input" value={effortChoice} onChange={(e) => setEffort(e.target.value)}>
                    <option value="">Default</option>
                    {selected.efforts.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                  </select>
                </label>}
              </div>
            </details>
            {pending?.phase === "failed" && <p role="alert" className="text-err">{pending.error}</p>}
            <div className="flex justify-end">
              <Button disabled={!canStart} onClick={() => void start(selected)}>{`Start ${selected.label}`}</Button>
            </div>
          </>}
          {localMac?.cli && <p className="text-caption text-muted-foreground">Or in any terminal, inside an approved folder: <code>lightbulb claude</code> or <code>lightbulb codex</code>.</p>}
          {localMac?.cli?.pathLine && <p className="text-caption text-muted-foreground">First add it to your PATH, then open a new terminal: <code className="select-all">{localMac.cli.pathLine}</code></p>}
        </div>
      </DialogContent>
    </Dialog>
  </section>;
}
