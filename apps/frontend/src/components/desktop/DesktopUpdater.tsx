"use client";

import { useCallback, useEffect, useState } from "react";

import { capture } from "@/lib/analytics";

// Sidebar footer for the desktop app. Replaces the static "isol8 v0.1"
// label with a clickable updater. In the regular browser this just
// renders the version label — the updater UI only appears when the
// Tauri webview is present.
//
// Tauri command surface (apps/desktop/src-tauri/src/lib.rs):
//   - check_for_updates → UpdateCheckResult { status: "up_to_date" | "available", ... }
//   - install_update    → downloads, verifies minisign signature, restarts
//
// The desktop app's tauri.conf.json version is bumped automatically by
// the release workflow on every `desktop-(dev-)?v*` tag; we read the
// currently-running version from `window.__TAURI_METADATA__.currentVersion`
// when available, falling back to a hardcoded label.

type TauriBridge = {
  core?: {
    invoke: <T = unknown>(
      cmd: string,
      args?: Record<string, unknown>,
    ) => Promise<T>;
  };
};

type CheckResult =
  | { status: "up_to_date" }
  | { status: "available"; version: string; notes: string | null };

type UiState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "up_to_date" }
  | {
      kind: "available";
      version: string;
      notes: string | null;
    }
  | { kind: "installing" }
  | { kind: "error"; message: string };

function getTauri(): TauriBridge | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const tauri = (window as any).__TAURI__ as TauriBridge | undefined;
  return tauri?.core?.invoke ? tauri : null;
}

export function DesktopUpdater({
  version = "0.1",
}: {
  version?: string;
}) {
  // Defer the Tauri detection to a post-mount effect so the server render
  // matches the client's initial render (no SSR/hydration mismatch when
  // running in a regular browser). After mount, `tauri` flips to non-null
  // on the desktop and the updater UI replaces the static label.
  const [tauri, setTauri] = useState<TauriBridge | null>(null);
  const [state, setState] = useState<UiState>({ kind: "idle" });

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR-safe init; see note above.
    setTauri(getTauri());
  }, []);

  const check = useCallback(async () => {
    if (!tauri?.core) return;
    setState({ kind: "checking" });
    capture("desktop_update_check_clicked");
    try {
      const result = await tauri.core.invoke<CheckResult>(
        "check_for_updates",
      );
      if (result.status === "available") {
        setState({
          kind: "available",
          version: result.version,
          notes: result.notes,
        });
      } else {
        setState({ kind: "up_to_date" });
      }
    } catch (e) {
      setState({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }, [tauri]);

  const install = useCallback(async () => {
    if (!tauri?.core) return;
    setState({ kind: "installing" });
    capture("desktop_update_install_clicked");
    try {
      await tauri.core.invoke("install_update");
      // The Rust side calls app.restart() after install — we'll never
      // reach this branch in practice, but keep it for the no-op case.
    } catch (e) {
      setState({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }, [tauri]);

  if (!tauri) {
    return <div className="version-text">isol8 v{version}</div>;
  }

  return (
    <div className="version-text" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span>isol8 v{version}</span>
      {state.kind === "idle" && (
        <button
          type="button"
          onClick={check}
          style={{
            background: "transparent",
            border: "none",
            padding: 0,
            color: "inherit",
            fontSize: "11px",
            textAlign: "left",
            cursor: "pointer",
            textDecoration: "underline",
            opacity: 0.7,
          }}
        >
          Check for updates
        </button>
      )}
      {state.kind === "checking" && <span style={{ fontSize: 11, opacity: 0.7 }}>Checking…</span>}
      {state.kind === "up_to_date" && (
        <span style={{ fontSize: 11, opacity: 0.7 }}>
          Up to date ·{" "}
          <button
            type="button"
            onClick={check}
            style={{ background: "transparent", border: "none", padding: 0, color: "inherit", fontSize: 11, cursor: "pointer", textDecoration: "underline" }}
          >
            recheck
          </button>
        </span>
      )}
      {state.kind === "available" && (
        <button
          type="button"
          onClick={install}
          title={state.notes ?? undefined}
          style={{
            background: "#16a34a",
            border: "none",
            padding: "4px 8px",
            color: "white",
            fontSize: 11,
            borderRadius: 4,
            cursor: "pointer",
            textAlign: "left",
          }}
        >
          Install v{state.version} & restart
        </button>
      )}
      {state.kind === "installing" && <span style={{ fontSize: 11, opacity: 0.7 }}>Installing…</span>}
      {state.kind === "error" && (
        <span style={{ fontSize: 11, color: "#dc2626" }}>
          Update error.{" "}
          <button
            type="button"
            onClick={check}
            style={{ background: "transparent", border: "none", padding: 0, color: "inherit", fontSize: 11, cursor: "pointer", textDecoration: "underline" }}
          >
            retry
          </button>
        </span>
      )}
    </div>
  );
}
