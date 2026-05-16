"use client";

import { useCallback, useEffect, useState } from "react";

import { capture } from "@/lib/analytics";

// Sidebar footer for the desktop app. Replaces the static "isol8 v0.1"
// label with a clickable updater. In the regular browser this just
// renders the version label — the updater UI only appears when the
// Tauri webview is present.
//
// Architecture note: as of feat/webview-updater (#673), the update CHECK
// runs in the webview rather than via Tauri's plugin-updater. The plugin
// uses its own reqwest client with no cookie jar, so it can't see the
// Vercel SSO cookie that gates dev.isol8.co's preview-protection. The
// webview IS Vercel-authenticated (same browser session that loaded the
// app), so fetching the manifest from it Just Works — no
// protection-bypass token, no scope mismatch.
//
// Tauri command surface (apps/desktop/src-tauri/src/lib.rs):
//   - updater_identity     → { target, current_version }
//   - apply_update_manifest → downloads from GitHub (public), verifies
//                            minisign signature, installs, restarts
//
// The bundle download URL inside the manifest is a github.com release
// asset (PUBLIC, no auth), so the Rust-side download doesn't need
// Vercel auth either.

type TauriBridge = {
  core?: {
    invoke: <T = unknown>(
      cmd: string,
      args?: Record<string, unknown>,
    ) => Promise<T>;
  };
};

type UpdaterIdentity = {
  target: string;
  current_version: string;
};

type UpdateManifest = {
  version: string;
  notes: string | null;
  pub_date: string | null;
  url: string;
  signature: string;
};

type UiState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "up_to_date" }
  | {
      kind: "available";
      manifest: UpdateManifest;
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
      // Ask Rust for the target triple + currently-installed version.
      // Both are baked at compile/install time so the webview can't
      // derive them reliably on its own.
      const identity = await tauri.core.invoke<UpdaterIdentity>(
        "updater_identity",
      );

      // Fetch the manifest with the webview's normal `fetch`, which
      // carries the Vercel SSO cookie automatically. Use a relative
      // path so it always resolves to the same host the webview was
      // loaded from (dev.isol8.co in dev, etc.).
      const res = await fetch(
        `/api/desktop/update/${encodeURIComponent(identity.target)}/${encodeURIComponent(identity.current_version)}`,
        {
          method: "GET",
          credentials: "include",
          headers: { Accept: "application/json" },
        },
      );

      if (res.status === 204) {
        setState({ kind: "up_to_date" });
        return;
      }
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        setState({
          kind: "error",
          message: `manifest fetch ${res.status}: ${text || res.statusText}`,
        });
        return;
      }

      const manifest = (await res.json()) as UpdateManifest;
      if (!manifest.version || !manifest.url || !manifest.signature) {
        setState({
          kind: "error",
          message: "manifest missing required fields",
        });
        return;
      }
      setState({ kind: "available", manifest });
    } catch (e) {
      setState({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }, [tauri]);

  const install = useCallback(async () => {
    if (!tauri?.core) return;
    if (state.kind !== "available") return;
    const manifest = state.manifest;
    setState({ kind: "installing" });
    capture("desktop_update_install_clicked");
    try {
      await tauri.core.invoke("apply_update_manifest", { manifest });
      // The Rust side calls app.restart() after install — we'll never
      // reach this branch in practice, but keep it for the no-op case.
    } catch (e) {
      setState({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }, [tauri, state]);

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
          title={state.manifest.notes ?? undefined}
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
          Install v{state.manifest.version} & restart
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
