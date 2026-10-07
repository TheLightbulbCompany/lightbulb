"use client";

import { useCallback, useEffect, useState } from "react";

import { capture } from "@/lib/analytics";

// Desktop footer: installed version comes from Rust, never the web bundle.
// Update checking/installing remains available only in the Tauri webview.
//
// Architecture note: as of feat/webview-updater (#673), the update CHECK
// runs in the webview rather than via Tauri's plugin-updater. The plugin
// uses its own reqwest client with no cookie jar, so it can't see the
// Vercel SSO cookie that gates dev.thelightbulb.company's preview-protection. The
// webview IS Vercel-authenticated (same browser session that loaded the
// app), so fetching the manifest from it Just Works — no
// protection-bypass token, no scope mismatch.
//
// Tauri command surface (apps/desktop/src-tauri/src/lib.rs):
//   - updater_identity     → { target, current_version }
//   - apply_update_manifest → downloads the manifest-selected bundle,
//                            verifies minisign, installs and restarts
//
// The download route resolves the selected private release asset server-side;
// the Rust client does not receive the GitHub token.

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

/** Diagnostic text stays useful without exposing expiring download credentials. */
function updateErrorMessage(error: unknown): string {
  return String(error instanceof Error ? error.message : error)
    .replace(/https?:\/\/[^\s"'<>]+/gi, "[download URL]")
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, "[token]")
    .slice(0, 500);
}

function getTauri(): TauriBridge | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const tauri = (window as any).__TAURI__ as TauriBridge | undefined;
  return tauri?.core?.invoke ? tauri : null;
}

export function DesktopUpdater({
  version,
}: {
  version?: string;
}) {
  // Defer the Tauri detection to a post-mount effect so the server render
  // matches the client's initial render (no SSR/hydration mismatch when
  // running in a regular browser). After mount, `tauri` flips to non-null
  // on the desktop and the updater UI replaces the static label.
  const [tauri, setTauri] = useState<TauriBridge | null>(null);
  const [state, setState] = useState<UiState>({ kind: "idle" });
  const [installedVersion, setInstalledVersion] = useState<string | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR-safe init; see note above.
    setTauri(getTauri());
  }, []);

  useEffect(() => {
    if (!tauri?.core) return;
    let alive = true;
    void tauri.core.invoke<UpdaterIdentity>("updater_identity")
      .then(identity => {
        if (alive && typeof identity.current_version === "string" && identity.current_version.trim()) {
          setInstalledVersion(identity.current_version);
        }
      })
      .catch(() => { /* Keep version unavailable; a web build number is not the installed app. */ });
    return () => { alive = false; };
  }, [tauri]);

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

      setInstalledVersion(identity.current_version);

      // Fetch the manifest with the webview's normal `fetch`, which
      // carries the Vercel SSO cookie automatically. Use a relative
      // path so it always resolves to the same host the webview was
      // loaded from (dev.thelightbulb.company in dev, etc.).
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
        setState({
          kind: "error",
          message: `Update check failed (HTTP ${res.status}).`,
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
        message: updateErrorMessage(e),
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
        message: updateErrorMessage(e),
      });
    }
  }, [tauri, state]);

  if (!tauri) {
    return <div className="version-text">{version ? `Lightbulb v${version}` : "Lightbulb"}</div>;
  }

  return (
    <div className="version-text" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span>{installedVersion ? `Lightbulb v${installedVersion}` : "Lightbulb · version unavailable"}</span>
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
            background: "var(--midnight)",
            border: "none",
            padding: "4px 8px",
            color: "var(--cream)",
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
        <div role="alert" style={{ fontSize: 11, color: "var(--err)" }}>
          Update error.{" "}
          <button
            type="button"
            onClick={check}
            style={{ background: "transparent", border: "none", padding: 0, color: "inherit", fontSize: 11, cursor: "pointer", textDecoration: "underline" }}
          >
            retry
          </button>
          <details style={{ marginTop: 4 }}>
            <summary style={{ cursor: "pointer" }}>Error details</summary>
            <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: "4px 0" }}>{state.message}</p>
          </details>
        </div>
      )}
    </div>
  );
}
