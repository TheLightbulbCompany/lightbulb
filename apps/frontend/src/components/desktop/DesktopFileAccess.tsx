"use client";

import { useCallback, useEffect, useState } from "react";

import { capture } from "@/lib/analytics";

// Settings → Desktop → File access panel.
//
// Shows the list of folders the cloud agent is allowed to read/write
// through file.fetch/write + dir.list/fetch + dir.fetch (and screen
// captures for screen.snapshot). The list is the user-managed safety
// boundary — the Rust node-host enforces it before every operation.
//
// In a regular browser this panel renders an informational message and
// nothing actionable, since the allowlist lives on the user's Mac. Only
// the Tauri webview can call the file_access_* IPC commands.

type TauriBridge = {
  core?: {
    invoke: <T = unknown>(
      cmd: string,
      args?: Record<string, unknown>,
    ) => Promise<T>;
  };
  dialog?: {
    open?: (options: Record<string, unknown>) => Promise<string | string[] | null>;
  };
};

function getTauri(): TauriBridge | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const t = (window as any).__TAURI__ as TauriBridge | undefined;
  return t?.core?.invoke ? t : null;
}

export function DesktopFileAccess() {
  const [tauri, setTauri] = useState<TauriBridge | null>(null);
  const [paths, setPaths] = useState<string[]>([]);
  const [defaultPath, setDefaultPath] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // SSR-safe init: window is browser-only and Tauri is only present in
    // the desktop webview. Setting state post-mount avoids hydration
    // mismatch with the server-rendered "browser fallback" message.
    setTauri(getTauri());
  }, []);

  const refresh = useCallback(async () => {
    if (!tauri?.core) return;
    try {
      const [list, def] = await Promise.all([
        tauri.core.invoke<string[]>("file_access_list_paths"),
        tauri.core.invoke<string>("file_access_default_path"),
      ]);
      setPaths(list ?? []);
      setDefaultPath(def ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [tauri]);

  useEffect(() => {
    if (tauri) refresh();
  }, [tauri, refresh]);

  const addFolder = useCallback(async () => {
    if (!tauri?.core) return;
    setBusy(true);
    setError(null);
    try {
      // Tauri's dialog plugin: open with directory=true returns the
      // selected absolute path (or null if cancelled). The path is
      // pre-canonicalized by macOS so our Rust side just persists it.
      const picked = (await tauri.core.invoke<string | null>("plugin:dialog|open", {
        options: { directory: true, multiple: false },
      })) as string | null;
      if (!picked) return;
      const canonical = await tauri.core.invoke<string>("file_access_add_path", {
        path: picked,
      });
      capture("desktop_file_access_added", { path: canonical });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [tauri, refresh]);

  const removeFolder = useCallback(
    async (path: string) => {
      if (!tauri?.core) return;
      setBusy(true);
      setError(null);
      try {
        await tauri.core.invoke<boolean>("file_access_remove_path", { path });
        capture("desktop_file_access_removed", { path });
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [tauri, refresh],
  );

  if (!tauri) {
    return (
      <div className="settings-panel" id="panel-desktop">
        <h1 className="settings-page-title">Desktop file access</h1>
        <p className="settings-page-desc">
          This setting lives on your Mac — open the Isol8 desktop app to
          manage which folders your agent can read or write.
        </p>
      </div>
    );
  }

  return (
    <div className="settings-panel" id="panel-desktop">
      <h1 className="settings-page-title">Desktop file access</h1>
      <p className="settings-page-desc">
        Folders below are the ONLY places your agent can read or write
        files through <code>file.fetch</code>/<code>file.write</code>/
        <code>dir.list</code>. Everything else returns{" "}
        <code>PERMISSION_DENIED</code>.
      </p>

      {defaultPath && (
        <p style={{ fontSize: 13, color: "#666", marginBottom: 12 }}>
          Default folder: <code>{defaultPath}</code> — created automatically
          on first launch so the agent always has somewhere to drop files.
        </p>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 16 }}>
        {paths.length === 0 ? (
          <p style={{ fontSize: 13, color: "#999" }}>(no folders configured)</p>
        ) : (
          paths.map((p) => (
            <div
              key={p}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "8px 12px",
                border: "1px solid #e5e5e5",
                borderRadius: 6,
                fontFamily: "monospace",
                fontSize: 13,
                gap: 8,
              }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {p}
              </span>
              <button
                type="button"
                onClick={() => removeFolder(p)}
                disabled={busy}
                style={{
                  border: "1px solid #dc2626",
                  background: "transparent",
                  color: "#dc2626",
                  padding: "4px 10px",
                  borderRadius: 4,
                  cursor: busy ? "not-allowed" : "pointer",
                  fontSize: 12,
                  flexShrink: 0,
                }}
              >
                Remove
              </button>
            </div>
          ))
        )}
      </div>

      <button
        type="button"
        onClick={addFolder}
        disabled={busy}
        style={{
          background: "#16a34a",
          border: "none",
          padding: "8px 16px",
          color: "white",
          borderRadius: 6,
          cursor: busy ? "not-allowed" : "pointer",
          fontSize: 14,
        }}
      >
        {busy ? "Working…" : "+ Add folder"}
      </button>

      {error && (
        <p style={{ marginTop: 12, color: "#dc2626", fontSize: 13 }}>
          Error: {error}
        </p>
      )}
    </div>
  );
}
