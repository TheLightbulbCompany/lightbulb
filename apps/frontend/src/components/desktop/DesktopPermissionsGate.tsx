"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Check, Radio, Terminal, type LucideIcon } from "lucide-react";

import { capture } from "@/lib/analytics";
import {
  BASIC_DESKTOP_PERMISSIONS,
  DEFAULT_DESKTOP_PERMISSIONS,
  DesktopPermissionSet,
  readDesktopPermissionGrant,
  writeDesktopPermissionGrant,
  desktopErrorMessage,
} from "@/lib/desktopPermissions";
import { subscribeDesktopPermissions } from "@/lib/desktopPermissionsSubscribe";
import { DESKTOP_PERMISSION_ITEMS, SHARE_LIVE_DESCRIPTION, SHARE_LIVE_LABEL, useLocalSessions } from "./LocalSessions";
import { LIVE_SHARING_OFFER_KEY } from "./ExplicitSharingNotice";
import { AlertDialog, AlertDialogContent, AlertDialogTitle, AlertDialogDescription } from "@/components/ui/alert-dialog";

type TauriBridge = {
  core?: {
    invoke: <T = unknown>(
      cmd: string,
      args?: Record<string, unknown>,
    ) => Promise<T>;
  };
};

function getTauri(): TauriBridge | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const tauri = (window as any).__TAURI__ as TauriBridge | undefined;
  return tauri?.core?.invoke ? tauri : null;
}

function getPermissionGateSnapshot(): boolean {
  return getTauri() !== null && readDesktopPermissionGrant() === null;
}

export function DesktopPermissionsGate() {
  const sharing = useLocalSessions(true);
  const shouldShowGate = useSyncExternalStore(
    subscribeDesktopPermissions,
    getPermissionGateSnapshot,
    () => false,
  );
  const [dismissed, setDismissed] = useState(false);
  const [permissions, setPermissions] = useState<DesktopPermissionSet>(
    DEFAULT_DESKTOP_PERMISSIONS,
  );
  const [choice, setChoice] = useState<boolean | null>(null);
  // Off unless its person turns it on (owner ruling 2026-10-08: all sharing is opt in). The switch
  // shows what this Mac's rc files say now (`stored`: on only on a Mac that turned it on before),
  // and pairing writes only what the person pressed. The switch and Continue wait for that read on
  // every Mac (the block can be there although this app's storage is new: a reinstall), so the box
  // never shows off over a Mac that is on. If it cannot be read the dialog says so, the switch
  // stays disabled and nothing is written.
  const [stored, setStored] = useState<boolean | null>(null);
  const [storedFailed, setStoredFailed] = useState(false);
  const [liveChoice, setLive] = useState<boolean | null>(null);
  const liveKnown = stored !== null;
  const live = liveChoice ?? stored ?? false;
  useEffect(() => {
    if (!shouldShowGate) return;
    let current = true;
    getTauri()?.core?.invoke<boolean>("shell_integration_status").then(
      on => { if (!current) return; if (typeof on === "boolean") setStored(on); else setStoredFailed(true); },
      () => { if (current) setStoredFailed(true); },
    );
    return () => { current = false; };
  }, [shouldShowGate]);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const operation = useRef(0);
  useEffect(() => {
    operation.current += 1;
    setDismissed(false); setChoice(null); setSaving(false); setSaveError(null);
    return () => { operation.current += 1; };
  }, [sharing.userId, sharing.orgId]);
  const sessionChoice = choice ?? sharing.state?.enrollment?.enabled ?? true;
  const checking = !sharing.state && !sharing.error;

  if ((!shouldShowGate && !saving && !saveError) || dismissed) return null;

  const toggle = (key: keyof DesktopPermissionSet) => {
    setPermissions((current) => ({
      ...current,
      [key]: !current[key],
    }));
  };

  const save = async (next: DesktopPermissionSet, mode: string) => {
    const current = ++operation.current;
    setSaving(true); setSaveError(null);
    // Pairing uses the existing node grant; sharing is a separate native command.
    if (shouldShowGate) {
      // The Mac saves the consent first; if it refuses, pairing stops here and says why.
      try { await writeDesktopPermissionGrant(next); }
      catch (error) {
        if (operation.current === current) { setSaveError(desktopErrorMessage(error)); setSaving(false); }
        return;
      }
      setPermissions(next);
    }
    if (mode === "basic") setChoice(false);
    capture("desktop_permissions_saved", {
      mode,
      screen: next.screen,
      shell: next.shell,
      browser: next.browser,
      files: next.files,
      paperclip: next.paperclip,
    });
    try {
      const enableSharing = mode !== "basic" && sessionChoice;
      let alreadyEnabled = false;
      if (enableSharing) {
        const invoke = getTauri()?.core?.invoke;
        if (!invoke) throw new Error("Desktop connection unavailable.");
        let ready = false;
        for (let attempt = 0; attempt < 30; attempt += 1) {
          const status = await invoke<NonNullable<typeof sharing.state>>("local_sessions_status");
          if (operation.current !== current) return;
          if (status.pairedWorkspaceId === sharing.orgId) {
            alreadyEnabled = Boolean(status.enrollment?.enabled && status.enrollment.ownerId === sharing.orgId && status.enrollment.humanUserId === sharing.userId);
            ready = true; break;
          }
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
        if (!ready) throw new Error("This Mac has not connected to this workspace yet. Check its connection and retry.");
      }
      if (!enableSharing || !alreadyEnabled) {
        if (!await sharing.setEnabled(enableSharing)) {
          if (operation.current === current) setSaveError("Sharing was not confirmed. Retry, or switch it off and continue.");
          return;
        }
      }
      // What the person pressed is always applied, whatever this dialog believes the rc files say
      // (off removes the block). Untouched: nothing is written, so a Mac keeps what it has and a
      // fresh one stays off.
      if (enableSharing && liveChoice !== null) {
        // Best effort: the switch stays in Settings if this build or the rc files refuse.
        await getTauri()?.core?.invoke("shell_integration_set", { enabled: liveChoice }).catch(() => {});
      }
      // This dialog made the offer, whatever the answer: the notice for Macs paired before the
      // switch existed (LiveSharingOffer) must not ask again.
      try { localStorage.setItem(LIVE_SHARING_OFFER_KEY, "1"); } catch { /* storage off */ }
      if (operation.current === current) setDismissed(true);
    } catch (error) {
      if (operation.current === current) setSaveError(desktopErrorMessage(error));
    } finally { if (operation.current === current) setSaving(false); }
  };
  const items: Array<{key:keyof DesktopPermissionSet | "sessions" | "live";label:string;description:string;Icon:LucideIcon}> = [
    ...DESKTOP_PERMISSION_ITEMS,
    {key:"sessions",label:"Share coding sessions",description:"Lets you share Claude Code, Codex, Cursor and Grok CLI sessions from this Mac with your workspace. On its own this shares nothing.",Icon:Terminal},
    ...(sessionChoice ? [{key:"live" as const,label:SHARE_LIVE_LABEL,description:SHARE_LIVE_DESCRIPTION,Icon:Radio}] : []),
  ];

  return (
    <AlertDialog open>
      <AlertDialogContent className="max-h-[90vh] max-w-[560px] overflow-y-auto rounded-lg border-edge bg-[var(--cream)] p-5 shadow-2xl">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <AlertDialogTitle
              className="text-title font-semibold text-midnight"
            >
              Connect this Mac
            </AlertDialogTitle>
            <AlertDialogDescription className="mt-1 text-sm text-[var(--midnight-muted)]">
              Choose once what agents may do on this Mac; nothing asks again per action. Change it any time in Settings.
            </AlertDialogDescription>
          </div>
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-midnight text-[var(--cream)]">
            <Check size={16} />
          </div>
        </div>

        <div className="grid gap-2">
          {items.map(({ key, label, description, Icon }) => {
            const enabled = key === "sessions" ? sessionChoice : key === "live" ? live : permissions[key];
            return (
              <button
                key={key}
                type="button"
                disabled={saving || (key === "live" ? !liveKnown : key !== "sessions" && !shouldShowGate)}
                onClick={() => key === "sessions" ? setChoice(!sessionChoice) : key === "live" ? setLive(!live) : toggle(key)}
                className={[
                  "flex w-full items-center gap-3 rounded-md border p-3 text-left transition",
                  enabled
                    ? "border-midnight bg-cream"
                    : "border-edge bg-cream-deep",
                ].join(" ")}
                aria-pressed={enabled}
              >
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-edge bg-[var(--cream)] text-midnight">
                  <Icon size={16} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-midnight">
                    {label}
                  </span>
                  <span className="mt-0.5 block text-xs leading-snug text-[var(--midnight-muted)]">
                    {description}
                  </span>
                </span>
                <span
                  className={[
                    "flex h-5 w-5 shrink-0 items-center justify-center rounded-sm border",
                    enabled
                      ? "border-midnight bg-midnight text-cream"
                      : "border-[var(--midnight-faint)] bg-transparent",
                  ].join(" ")}
                  aria-hidden="true"
                >
                  {enabled && <Check size={12} />}
                </span>
              </button>
            );
          })}
        </div>
        <p className="text-xs leading-relaxed text-[var(--midnight-muted)]">
          Nothing is shared until you start a session with Lightbulb, choose Share to Lightbulb in it, or turn on “{SHARE_LIVE_LABEL}”. Every member of your workspace, and its AI agents, can see a shared session’s live terminal and conversation, secrets it prints included. “Anyone with the link” is a separate choice you make per session, and you can switch any session back to Only me. This choice is saved; change it in Settings.
        </p>
        {sessionChoice && storedFailed && !liveKnown && <p role="status" className="text-xs text-[var(--midnight-muted)]">Could not read whether “{SHARE_LIVE_LABEL}” is on for this Mac. It is left as it is: check it in Settings.</p>}
        {checking && <p role="status" className="text-xs text-[var(--midnight-muted)]">Checking local-session sharing… You can continue with Basic only.</p>}
        {(saveError || sharing.error) && <p role="alert" className="text-sm text-destructive">{sharing.error || saveError}</p>}

        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            disabled={saving}
            onClick={() => void save(BASIC_DESKTOP_PERMISSIONS, "basic")}
            className="rounded-md border border-edge px-4 py-2 text-sm font-medium text-[var(--midnight-soft)] hover:bg-cream"
          >
            {shouldShowGate ? "Basic only" : "Continue without sharing"}
          </button>
          <button
            type="button"
            disabled={saving || checking || !sharing.userId || !sharing.orgId || (shouldShowGate && sessionChoice && !liveKnown && !storedFailed)}
            onClick={() => void save(permissions, "custom")}
            className="rounded-md bg-midnight px-4 py-2 text-sm font-medium text-cream hover:bg-[var(--midnight-soft)]"
          >
            {saving ? "Connecting…" : "Continue"}
          </button>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}
