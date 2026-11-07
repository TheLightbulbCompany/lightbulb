"use client";

import { useCallback, useEffect, useState } from "react";

import { AlertCircle, Loader2, Pencil, Plus, RefreshCw, Server, Trash2 } from "lucide-react";

import { capture } from "@/lib/analytics";
import { parseLocalMcpConfig, type LocalMcpServer } from "@/lib/localMcp";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * Settings → Local MCP servers.
 *
 * The desktop app can run MCP servers as processes on the user's own Mac and
 * expose their tools to the agent. This panel is the config surface for that
 * map. It is forked from control/panels/McpServersTab (the container-side,
 * REST-backed tab) and keeps its paste-a-config idiom, edit-by-repopulating,
 * and the server card list — with three differences:
 *
 *  - The transport is Tauri IPC, not REST: `local_mcp_list` / `local_mcp_set`
 *    (whole-map replace) / `local_mcp_remove`.
 *  - No auth dimension. The container tab 403s non-admin org members because
 *    the entries carry org-wide secrets; this is one person's own laptop, so
 *    the only gate is "are we in the desktop app at all".
 *  - Every write restarts the node host, briefly dropping the agent's
 *    connection to this Mac. That's stated, not hidden behind a success toast.
 */

// Same local bridge-detection pattern as components/desktop/DesktopFileAccess:
// the Tauri webview injects window.__TAURI__; a regular browser never has it.
type TauriInvoke = <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;

function getTauriInvoke(): TauriInvoke | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const invoke = (window as any).__TAURI__?.core?.invoke as TauriInvoke | undefined;
  return invoke ?? null;
}

const PLACEHOLDER_CONFIG = `{
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/Documents"]
  },
  "local-http": {
    "url": "http://localhost:3000/mcp",
    "transport": "streamable-http"
  }
}`;

// Shown after every successful write. There is no watcher on the other side —
// `local_mcp_set`/`local_mcp_remove` restart the node host — and nothing in the
// IPC contract reports when it is back, so this notice states what happened and
// stays put until the next action rather than faking a resolved spinner.
const RESTART_NOTICE =
  "Saved. Changing servers restarts the local agent host, so this Mac's tools drop off for a few seconds and then reconnect on their own.";

function errText(e: unknown): string {
  // Tauri invoke rejects with a plain string, not an Error.
  const raw = e instanceof Error ? e.message : String(e);
  // This pane ships with the WEB deploy; the commands it calls ship with the
  // DESKTOP build. The app loads the remote URL, so the pane appears inside
  // already-installed builds the moment the frontend deploys — and every
  // invoke then rejects with Tauri's "not found" until the user picks up a DMG
  // carrying `local_mcp_*`. They pick that up whenever they next relaunch, so
  // the window is days, not minutes. Say what to do instead of surfacing the
  // raw string.
  if (/not found|unknown command|missing .*command/i.test(raw)) {
    return "This needs a newer version of the Lightbulb desktop app. Check for updates, then reopen Settings.";
  }
  return raw;
}

export function LocalMcpServers() {
  // `null` until mount. window is browser-only and Tauri lives only in the
  // desktop webview, so reading it during render would hydrate-mismatch the
  // server-rendered browser fallback (DesktopFileAccess documents the same).
  const [invoke, setInvoke] = useState<TauriInvoke | null>(null);
  const [mounted, setMounted] = useState(false);

  const [servers, setServers] = useState<Record<string, LocalMcpServer>>({});
  const [loading, setLoading] = useState(true);
  const [configInput, setConfigInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    setInvoke(() => getTauriInvoke());
    setMounted(true);
  }, []);

  const refresh = useCallback(async () => {
    if (!invoke) return;
    setLoading(true);
    try {
      const list = await invoke<Record<string, LocalMcpServer>>("local_mcp_list");
      setServers(list ?? {});
      setError(null);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, [invoke]);

  useEffect(() => {
    if (invoke) refresh();
    else if (mounted) setLoading(false);
  }, [invoke, mounted, refresh]);

  const handleSave = useCallback(async () => {
    if (!invoke || !configInput.trim()) return;

    const parsed = parseLocalMcpConfig(configInput);
    if (!parsed.ok) {
      setError(parsed.error);
      setRestarting(false);
      return;
    }

    setSaving(true);
    setError(null);
    setRestarting(false);
    // `local_mcp_set` replaces the WHOLE map, so an add has to send the
    // existing entries back with it or it would silently delete them.
    const merged = { ...servers, ...parsed.servers };
    try {
      await invoke("local_mcp_set", { servers: merged });
      setServers(merged);
      setConfigInput("");
      setRestarting(true);
      capture("local_mcp_servers_saved", { count: Object.keys(parsed.servers).length });
    } catch (e) {
      setError(errText(e));
    } finally {
      setSaving(false);
    }
  }, [invoke, configInput, servers]);

  const handleRemove = useCallback(
    async (name: string) => {
      if (!invoke) return;
      setRemoving(name);
      setError(null);
      setRestarting(false);
      try {
        await invoke<boolean>("local_mcp_remove", { name });
        // The command returns false when the entry was already gone; either
        // way the desired end state is the same, so drop it locally.
        setServers((prev) => {
          const next = { ...prev };
          delete next[name];
          return next;
        });
        setRestarting(true);
        capture("local_mcp_server_removed", { server_name: name });
      } catch (e) {
        setError(errText(e));
      } finally {
        setRemoving(null);
      }
    },
    [invoke],
  );

  const handleEdit = useCallback(
    (name: string) => {
      const entry = servers[name];
      if (!entry) return;
      setConfigInput(JSON.stringify({ [name]: entry }, null, 2));
      setError(null);
      setRestarting(false);
    },
    [servers],
  );

  // Pre-mount: render the frame but nothing that differs between the desktop
  // app and a browser, so the first client paint matches the server's.
  if (!mounted) {
    return <div className="settings-panel" id="panel-local-mcp" />;
  }

  if (!invoke) {
    return (
      <div className="settings-panel" id="panel-local-mcp">
        <h1 className="settings-page-title">Local MCP servers</h1>
        <p className="settings-page-desc">
          These servers run on your Mac, so they are managed from the Lightbulb
          desktop app — open it to add or remove them.
        </p>
      </div>
    );
  }

  const serverEntries = Object.entries(servers);

  return (
    <div className="settings-panel" id="panel-local-mcp">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="settings-page-title">Local MCP servers</h1>
          <p className="settings-page-desc">
            MCP servers listed here run on this Mac and their tools are handed to
            your agent. They never leave the machine — nothing here is uploaded.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={refresh} disabled={loading} aria-label="Refresh">
          <RefreshCw size={16} />
        </Button>
      </div>

      {/* Paste config */}
      <div className="space-y-2">
        <p className="text-xs text-muted-foreground">
          Paste a server config. A <code>url</code> with no <code>transport</code>{" "}
          is saved as <code>streamable-http</code>.
        </p>
        <textarea
          className="w-full h-44 rounded-lg border border-border bg-background p-3 text-xs font-mono resize-y"
          placeholder={PLACEHOLDER_CONFIG}
          value={configInput}
          onChange={(e) => setConfigInput(e.target.value)}
          aria-label="Local MCP server configuration"
        />
        <div className="flex items-center gap-3">
          <Button size="sm" className="text-xs gap-1.5" onClick={handleSave} disabled={saving || !configInput.trim()}>
            {saving ? (
              <Loader2 size={12} strokeWidth={1.75} className="animate-spin" />
            ) : (
              <Plus size={12} strokeWidth={1.75} />
            )}
            Save &amp; restart
          </Button>
        </div>

        {error && (
          <div className="flex items-start gap-1.5 text-xs text-destructive" role="alert">
            <AlertCircle size={12} strokeWidth={1.75} className="shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {restarting && !error && (
          <div className="flex items-start gap-1.5 text-xs text-muted-foreground" role="status">
            <RefreshCw size={12} strokeWidth={1.75} className="shrink-0 mt-0.5" />
            <span>{RESTART_NOTICE}</span>
          </div>
        )}
      </div>

      {/* Configured servers */}
      <div className="space-y-2 mt-5">
        <h3 className="label-caps text-muted-foreground">
          On this Mac ({serverEntries.length})
        </h3>

        {loading && <Loader2 size={16} className="animate-spin text-muted-foreground" />}

        {!loading && serverEntries.length === 0 && (
          <p className="text-xs text-muted-foreground">No local MCP servers yet.</p>
        )}

        {!loading &&
          serverEntries.map(([name, server]) => (
            <div key={name} className="rounded-lg border border-border p-4 space-y-2 bg-cream-deep">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <Server size={16} className="text-muted-foreground shrink-0" />
                    <h4 className="text-sm font-medium truncate">{name}</h4>
                    {server.enabled === false && (
                      <span className="text-2xs text-muted-foreground">disabled</span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1 font-mono break-all">
                    {server.command
                      ? `${server.command}${server.args?.length ? ` ${server.args.join(" ")}` : ""}`
                      : `${server.url ?? ""}${server.transport ? ` (${server.transport})` : ""}`}
                  </p>
                </div>
                <div className="flex items-center gap-1">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 w-7 p-0"
                        onClick={() => handleEdit(name)}
                        aria-label={`Edit ${name}`}
                      >
                        <Pencil size={12} strokeWidth={1.75} />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Edit</TooltipContent>
                  </Tooltip>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 w-7 p-0 text-destructive hover:text-destructive"
                        onClick={() => handleRemove(name)}
                        disabled={removing !== null}
                        aria-label={`Remove ${name}`}
                      >
                        {removing === name ? (
                          <Loader2 size={12} strokeWidth={1.75} className="animate-spin" />
                        ) : (
                          <Trash2 size={12} strokeWidth={1.75} />
                        )}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Remove</TooltipContent>
                  </Tooltip>
                </div>
              </div>

              {server.env && Object.keys(server.env).length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(server.env).map(([key, val]) => (
                    <span
                      key={key}
                      className="inline-flex items-center px-1.5 py-0.5 rounded-sm text-2xs font-mono bg-muted text-muted-foreground"
                    >
                      {key}: {val ? "set" : "not set"}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
      </div>
    </div>
  );
}
