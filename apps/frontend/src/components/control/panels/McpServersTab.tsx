"use client";

import { useState, useEffect, useCallback } from "react";

import { capture } from "@/lib/analytics";
import {
  Loader2,
  RefreshCw,
  Trash2,
  Plus,
  Pencil,
  AlertCircle,
  CheckCircle2,
  Server,
} from "lucide-react";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

interface McpServer {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  transport?: string;
  url?: string;
  [key: string]: unknown;
}

interface ServersResponse {
  servers: Record<string, McpServer>;
  managed_keys?: string[];
}

function serverSummary(server: McpServer): string {
  if (server.command) {
    const args = Array.isArray(server.args) ? server.args.join(" ") : "";
    return args ? `${server.command} ${args}` : server.command;
  }
  if (!server.url) return "Invalid server configuration";
  try {
    const host = new URL(server.url).host;
    return host ? `${server.transport ?? "Remote MCP"} · ${host}` : "Remote MCP server";
  } catch {
    return "Remote MCP server";
  }
}

const PLACEHOLDER_CONFIG = `{
  "server-name": {
    "command": "npx",
    "args": ["-y", "@scope/mcp-server"],
    "env": {
      "API_KEY": "your-key-here"
    }
  }
}`;

export function McpServersTab(props: { agentId?: string }) {
  void props;
  const api = useApi();
  const [servers, setServers] = useState<Record<string, McpServer>>({});
  // null also covers an older backend during rollout: existing rows stay read-only.
  const [managedKeys, setManagedKeys] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [fetchError, setFetchError] = useState(false);
  const [configInput, setConfigInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [status, setStatus] = useState<{ type: "success" | "error"; message: string } | null>(null);

  const fetchServers = useCallback(async () => {
    setLoading(true);
    setForbidden(false);
    setFetchError(false);
    try {
      const resp = (await api.get("/integrations/mcp/servers")) as ServersResponse;
      setServers(resp.servers ?? {});
      setManagedKeys(resp.managed_keys ?? null);
    } catch (err) {
      // Non-admin org members get 403 — entries carry env secrets only the
      // admin set up. Show an empty-state with a clear message instead of
      // a silent fail.
      if ((err as { status?: number }).status === 403) {
        setForbidden(true);
        setServers({});
      } else {
        // H4: this used to be console.error-only, rendering the same empty
        // list a first-load spinner shows — surface it so a broken fetch
        // reads as an error, not "no servers configured".
        console.error("Failed to fetch MCP servers:", err);
        setFetchError(true);
      }
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchServers();
  }, [fetchServers]);

  const handleAdd = async () => {
    if (!configInput.trim()) return;

    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(configInput);
    } catch {
      setStatus({ type: "error", message: "Invalid JSON. Please check your configuration." });
      return;
    }
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object" || Object.keys(parsed).length === 0) {
      setStatus({ type: "error", message: "Paste an object containing at least one server." });
      return;
    }

    // The backend accepts either a local command or a remote transport + URL.
    for (const [name, entry] of Object.entries(parsed)) {
      if (!entry || typeof entry !== "object") {
        setStatus({ type: "error", message: `Server "${name}" must be an object.` });
        return;
      }
      const e = entry as Record<string, unknown>;
      if (managedKeys?.includes(name)) {
        setStatus({ type: "error", message: `Server "${name}" is managed through Connections.` });
        return;
      }
      const local = typeof e.command === "string" && !!e.command.trim();
      const remote = typeof e.transport === "string" && !!e.transport.trim()
        && typeof e.url === "string" && !!e.url.trim();
      if (local === remote) {
        setStatus({ type: "error", message: `Server "${name}" needs either a command or a transport and URL.` });
        return;
      }
    }

    setSaving(true);
    setStatus(null);
    try {
      // PATCH writes only manual entries; PUT would send connected providers'
      // merged blocks back as owner config, including brokered remote servers.
      for (const [name, entry] of Object.entries(parsed)) {
        const resp = (await api.patch(
          `/integrations/mcp/servers/${encodeURIComponent(name)}`,
          entry,
        )) as ServersResponse;
        setServers(resp.servers ?? {});
      }
      setConfigInput("");
      capture("mcp_server_added", { server_count: Object.keys(parsed).length });
      setStatus({ type: "success", message: "Server(s) added successfully." });
      setTimeout(() => setStatus(null), 3000);
    } catch (err) {
      // A multi-entry paste can partly succeed. Re-read without discarding the
      // draft so retrying safely overwrites the entries already written.
      await fetchServers();
      notifyApiError(err, "Failed to save MCP server. Please try again.");
    } finally {
      setSaving(false);
    }
  };

  const handleRemove = async (name: string) => {
    if (managedKeys === null || managedKeys.includes(name)) return;
    setDeleting(name);
    try {
      const resp = (await api.del(`/integrations/mcp/servers/${encodeURIComponent(name)}`)) as ServersResponse;
      setServers(resp.servers ?? {});
      capture("mcp_server_removed");
    } catch (err) {
      notifyApiError(err, "Failed to remove MCP server. Please try again.");
    } finally {
      setDeleting(null);
    }
  };

  const handleEdit = (name: string) => {
    if (managedKeys === null || managedKeys.includes(name)) return;
    const entry = servers[name];
    if (!entry) return;
    setConfigInput(JSON.stringify({ [name]: entry }, null, 2));
  };

  const serverEntries = Object.entries(servers);

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 size={20} className="animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (forbidden) {
    return (
      <div className="p-6 space-y-3">
        <h2 className="font-editorial-serif text-title">MCP Servers</h2>
        <div className="rounded-lg border border-border bg-cream-deep p-6 text-center">
          <Server size={20} className="mx-auto mb-2 text-[var(--midnight-faint)]" />
          <p className="text-sm font-medium">Org admins only.</p>
          <p className="text-xs text-muted-foreground mt-1 max-w-sm mx-auto">
            MCP server entries carry per-server <code>env</code> secrets only
            the admin set up. Ask your org admin to manage them.
          </p>
        </div>
      </div>
    );
  }

  if (fetchError) {
    return (
      <div className="p-6 space-y-3">
        <h2 className="font-editorial-serif text-title">MCP Servers</h2>
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-cream border border-err">
          <AlertCircle size={16} className="text-err shrink-0" />
          <span className="text-xs text-err">Failed to load MCP servers.</span>
          <Button variant="outline" size="sm" className="ml-auto h-6 text-xs" onClick={fetchServers}>
            Retry
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="font-editorial-serif text-title">MCP Servers</h2>
        <Button variant="ghost" size="sm" onClick={fetchServers}>
          <RefreshCw size={16} />
        </Button>
      </div>

      {/* Paste config section */}
      <div className="space-y-2">
        <p className="text-xs text-muted-foreground">
          Add a local command or remote transport and URL by pasting its configuration:
        </p>
        <textarea
          className="w-full h-36 rounded-lg border border-border bg-background p-3 text-xs font-mono resize-y"
          placeholder={PLACEHOLDER_CONFIG}
          value={configInput}
          onChange={(e) => setConfigInput(e.target.value)}
        />
        <div className="flex items-center gap-3">
          <Button
            size="sm"
            className="text-xs gap-1.5"
            onClick={handleAdd}
            disabled={saving || !configInput.trim()}
          >
            {saving ? (
              <Loader2 size={12} strokeWidth={1.75} className="animate-spin" />
            ) : (
              <Plus size={12} strokeWidth={1.75} />
            )}
            Add Server
          </Button>
          {status && (
            <div className={`flex items-center gap-1.5 text-xs ${status.type === "error" ? "text-destructive" : "text-ok"}`}>
              {status.type === "error" ? (
                <AlertCircle size={12} strokeWidth={1.75} className="shrink-0" />
              ) : (
                <CheckCircle2 size={12} strokeWidth={1.75} className="shrink-0" />
              )}
              {status.message}
            </div>
          )}
        </div>
      </div>

      {/* Configured servers list */}
      <div className="space-y-2">
        <h3 className="label-caps text-muted-foreground">
          Configured Servers ({serverEntries.length})
        </h3>

        {serverEntries.length === 0 && (
          <p className="text-xs text-muted-foreground">No MCP servers configured yet.</p>
        )}

        {serverEntries.map(([name, server]) => (
          <div
            key={name}
            className="rounded-lg border border-border p-4 space-y-2 bg-cream-deep"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <Server size={16} className="text-muted-foreground shrink-0" />
                  <h4 className="text-sm font-medium truncate">{name}</h4>
                </div>
                <p className="text-xs text-muted-foreground mt-1 font-mono">
                  {serverSummary(server)}
                </p>
              </div>
              {managedKeys === null || managedKeys.includes(name) ? (
                <span className="text-xs text-muted-foreground">
                  {managedKeys === null ? "Read-only until refreshed" : "Managed through Connections"}
                </span>
              ) : (
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
                        disabled={deleting !== null}
                        aria-label={`Remove ${name}`}
                      >
                        {deleting === name ? (
                          <Loader2 size={12} strokeWidth={1.75} className="animate-spin" />
                        ) : (
                          <Trash2 size={12} strokeWidth={1.75} />
                        )}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Remove</TooltipContent>
                  </Tooltip>
                </div>
              )}
            </div>

            {/* Env vars */}
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
