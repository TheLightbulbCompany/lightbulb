"use client";

import { useCallback, useEffect, useState } from "react";

import { capture } from "@/lib/analytics";
import { desktopErrorMessage } from "@/lib/desktopPermissions";

// Settings → Desktop → "Approved folders" card.
//
// The folders the agent may read/write through file.fetch/write + dir.list/fetch
// + dir.fetch (and screen captures for screen.snapshot). The list is the
// user-managed safety boundary — the Rust node-host enforces it before every
// operation. Renders nothing outside the desktop app: only the Tauri webview can
// call the file_access_* IPC commands (DesktopSettings shows the download card).

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
      const list = await tauri.core.invoke<string[]>("file_access_list_paths");
      setPaths(list ?? []);
      setError(null);
    } catch (e) {
      setError(desktopErrorMessage(e));
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
      await tauri.core.invoke<string>("file_access_add_path", {
        path: picked,
      });
      capture("desktop_file_access_added");
      await refresh();
    } catch (e) {
      setError(desktopErrorMessage(e));
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
        capture("desktop_file_access_removed");
        await refresh();
      } catch (e) {
        setError(desktopErrorMessage(e));
      } finally {
        setBusy(false);
      }
    },
    [tauri, refresh],
  );

  if (!tauri) return null;

  return (
    <section className="settings-card" aria-labelledby="approved-folders-heading">
      <div className="settings-card-header settings-card-header--rows">
        <div>
          <h2 className="settings-card-title" id="approved-folders-heading">Approved folders</h2>
          <div className="settings-card-desc">
            Agents read and write files only inside these folders. Sessions you start from Lightbulb also run in them.
          </div>
        </div>
      </div>
      {paths.length === 0 ? (
        <p className="settings-toggle-desc">No folders yet.</p>
      ) : (
        <ul className="settings-folder-list">
          {paths.map((p) => (
            <li key={p} className="settings-folder">
              <span className="settings-folder-path" title={p}>{p}</span>
              <button type="button" className="settings-btn-text" onClick={() => removeFolder(p)} disabled={busy} aria-label={`Remove ${p}`}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="settings-row-actions">
        <button type="button" className="settings-btn-outline" onClick={addFolder} disabled={busy}>
          {busy ? "Working…" : "Add folder"}
        </button>
      </div>
      {error ? <p className="settings-row-error" role="alert">{error}</p> : null}
    </section>
  );
}
