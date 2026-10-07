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
  // On by default (owner ruling 2026-10-07): the item says which files it edits, and one click turns it off.
  const [live, setLive] = useState(true);
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
      writeDesktopPermissionGrant(next);
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
      if (enableSharing && live) {
        // Best effort: the switch stays in Settings if this build or the rc files refuse.
        await getTauri()?.core?.invoke("shell_integration_set", { enabled: true }).catch(() => {});
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
    {key:"sessions",label:"Share Claude & Codex sessions",description:"Let people in your workspace conversations view and steer the Claude Code and Codex sessions on this Mac.",Icon:Terminal},
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
                disabled={saving || (key !== "sessions" && key !== "live" && !shouldShowGate)}
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
          Sharing includes active sessions’ existing transcripts and new sessions. Current human members of conversations you share in this workspace can view and steer them; contributions can be visible across conversations. Personal memory files are not uploaded. Tool commands, output previews and file changes are shared as the harness shows them. Enrollment is saved and resumes automatically when you reopen Lightbulb. You can disable sharing in Settings.
        </p>
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
            disabled={saving || checking || !sharing.userId || !sharing.orgId}
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
