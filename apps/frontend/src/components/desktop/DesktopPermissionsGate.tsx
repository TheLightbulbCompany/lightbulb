"use client";

import { useState, useSyncExternalStore } from "react";
import {
  Check,
  FolderOpen,
  Globe2,
  Monitor,
  Terminal,
  type LucideIcon,
} from "lucide-react";

import { capture } from "@/lib/analytics";
import {
  BASIC_DESKTOP_PERMISSIONS,
  DEFAULT_DESKTOP_PERMISSIONS,
  DesktopPermissionSet,
  readDesktopPermissionGrant,
  writeDesktopPermissionGrant,
} from "@/lib/desktopPermissions";
import { subscribeDesktopPermissions } from "@/lib/desktopPermissionsSubscribe";

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

const ITEMS: Array<{
  key: keyof DesktopPermissionSet;
  label: string;
  description: string;
  Icon: LucideIcon;
}> = [
  {
    key: "browser",
    label: "Browser",
    description: "Open and operate browser sessions through the paired node.",
    Icon: Globe2,
  },
  {
    key: "shell",
    label: "Terminal",
    description: "Run commands through the node-host command surface.",
    Icon: Terminal,
  },
  {
    key: "files",
    label: "Files",
    description: "Read, list, and write files in folders you allow on this Mac.",
    Icon: FolderOpen,
  },
  {
    key: "screen",
    label: "Screen",
    description: "Capture snapshots for visual context and troubleshooting.",
    Icon: Monitor,
  },
];

export function DesktopPermissionsGate() {
  const shouldShowGate = useSyncExternalStore(
    subscribeDesktopPermissions,
    getPermissionGateSnapshot,
    () => false,
  );
  const [dismissed, setDismissed] = useState(false);
  const [permissions, setPermissions] = useState<DesktopPermissionSet>(
    DEFAULT_DESKTOP_PERMISSIONS,
  );

  if (!shouldShowGate || dismissed) return null;

  const toggle = (key: keyof DesktopPermissionSet) => {
    setPermissions((current) => ({
      ...current,
      [key]: !current[key],
    }));
  };

  const save = (next: DesktopPermissionSet, mode: string) => {
    writeDesktopPermissionGrant(next);
    capture("desktop_permissions_saved", {
      mode,
      screen: next.screen,
      shell: next.shell,
      browser: next.browser,
      files: next.files,
    });
    setDismissed(true);
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="desktop-permissions-title"
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/45 px-4"
    >
      <div className="w-full max-w-[560px] rounded-lg border border-[#ded8cc] bg-[#faf7f2] p-5 shadow-2xl">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2
              id="desktop-permissions-title"
              className="text-[20px] font-semibold text-[#1f1b16]"
            >
              Connect this Mac
            </h2>
            <p className="mt-1 text-sm leading-5 text-[#6f675c]">
              Choose what agents can use on this computer. You can adjust file
              folders later in Settings.
            </p>
          </div>
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-[#1f1b16] text-[#faf7f2]">
            <Check size={18} />
          </div>
        </div>

        <div className="grid gap-2">
          {ITEMS.map(({ key, label, description, Icon }) => {
            const enabled = permissions[key];
            return (
              <button
                key={key}
                type="button"
                onClick={() => toggle(key)}
                className={[
                  "flex w-full items-center gap-3 rounded-md border p-3 text-left transition",
                  enabled
                    ? "border-[#1f1b16] bg-white"
                    : "border-[#ded8cc] bg-[#f4efe7]",
                ].join(" ")}
                aria-pressed={enabled}
              >
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-[#ded8cc] bg-[#faf7f2] text-[#1f1b16]">
                  <Icon size={17} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-[#1f1b16]">
                    {label}
                  </span>
                  <span className="mt-0.5 block text-xs leading-4 text-[#6f675c]">
                    {description}
                  </span>
                </span>
                <span
                  className={[
                    "flex h-5 w-5 shrink-0 items-center justify-center rounded border",
                    enabled
                      ? "border-[#1f1b16] bg-[#1f1b16] text-white"
                      : "border-[#b9afa1] bg-transparent",
                  ].join(" ")}
                  aria-hidden="true"
                >
                  {enabled && <Check size={13} />}
                </span>
              </button>
            );
          })}
        </div>

        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={() => save(BASIC_DESKTOP_PERMISSIONS, "basic")}
            className="rounded-md border border-[#cfc6b8] px-4 py-2 text-sm font-medium text-[#4d463d] hover:bg-white"
          >
            Basic only
          </button>
          <button
            type="button"
            onClick={() => save(permissions, "custom")}
            className="rounded-md bg-[#1f1b16] px-4 py-2 text-sm font-medium text-white hover:bg-[#383126]"
          >
            Connect Mac
          </button>
        </div>
      </div>
    </div>
  );
}
