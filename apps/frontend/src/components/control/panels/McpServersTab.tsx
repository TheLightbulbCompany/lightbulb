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
import { Button } from "@/components/ui/button";

interface McpServer {
  command: string;
  args?: string[];
  env?: Record<string, string>;
  [key: string]: unknown;
}

interface ServersResponse {
  servers: Record<string, McpServer>;
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
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [configInput, setConfigInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [status, setStatus] = useState<{ type: "success" | "error"; message: string } | null>(null);

  const fetchServers = useCallback(async () => {
    setLoading(true);
    setForbidden(false);
    try {
      const resp = (await api.get("/integrations/mcp/servers")) as ServersResponse;
      setServers(resp.servers ?? {});
    } catch (err) {
      // Non-admin org members get 403 — entries carry env secrets only the
      // admin set up. Show an empty-state with a clear message instead of
      // a silent fail.
      if ((err as { status?: number }).status === 403) {
        setForbidden(true);
        setServers({});
      } else {
        console.error("Failed to fetch MCP servers:", err);
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

    // Validate each entry has a command string
    for (const [name, entry] of Object.entries(parsed)) {
      if (!entry || typeof entry !== "object") {
        setStatus({ type: "error", message: `Server "${name}" must be an object.` });
        return;
      }
      const e = entry as Record<string, unknown>;
      if (!e.command || typeof e.command !== "string") {
        setStatus({ type: "error", message: `Server "${name}" must have a "command" string field.` });
        return;
      }
    }

    setSaving(true);
    setStatus(null);
    try {
      const merged = { ...servers, ...parsed };
      const resp = (await api.put("/integrations/mcp/servers", { servers: merged })) as ServersResponse;
      setServers(resp.servers ?? {});
      setConfigInput("");
      capture("mcp_server_added", { server_name: Object.keys(parsed).join(", ") });
      setStatus({ type: "success", message: "Server(s) added successfully." });
      setTimeout(() => setStatus(null), 3000);
    } catch (err) {
      console.error("Failed to save MCP servers:", err);
      setStatus({ type: "error", message: "Failed to save. Please try again." });
    } finally {
      setSaving(false);
    }
  };

  const handleRemove = async (name: string) => {
    setDeleting(name);
    try {
      const resp = (await api.del(`/integrations/mcp/servers/${encodeURIComponent(name)}`)) as ServersResponse;
      setServers(resp.servers ?? {});
      capture("mcp_server_removed", { server_name: name });
    } catch (err) {
      console.error("Failed to remove MCP server:", err);
    } finally {
      setDeleting(null);
    }
  };

  const handleEdit = (name: string) => {
    const entry = servers[name];
    if (!entry) return;
    setConfigInput(JSON.stringify({ [name]: entry }, null, 2));
  };

  const serverEntries = Object.entries(servers);

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (forbidden) {
    return (
      <div className="p-6 space-y-3">
        <h2 className="text-lg font-semibold">MCP Servers</h2>
        <div className="rounded-lg border border-border bg-card/30 p-6 text-center">
          <Server className="h-8 w-8 mx-auto mb-2 text-muted-foreground/60" />
          <p className="text-sm font-medium">Org admins only.</p>
          <p className="text-xs text-muted-foreground mt-1 max-w-sm mx-auto">
            MCP server entries carry per-server <code>env</code> secrets only
            the admin set up. Ask your org admin to manage them.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">MCP Servers</h2>
        <Button variant="ghost" size="sm" onClick={fetchServers}>
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </div>

      {/* Paste config section */}
      <div className="space-y-2">
        <p className="text-xs text-muted-foreground">
          Add a new MCP server by pasting its configuration:
        </p>
        <textarea
          className="w-full h-36 rounded-lg border border-border bg-background p-3 text-xs font-mono resize-y focus:outline-none focus:ring-1 focus:ring-ring"
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
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Plus className="h-3 w-3" />
            )}
            Add Server
          </Button>
          {status && (
            <div className={`flex items-center gap-1.5 text-xs ${status.type === "error" ? "text-destructive" : "text-green-500"}`}>
              {status.type === "error" ? (
                <AlertCircle className="h-3 w-3 flex-shrink-0" />
              ) : (
                <CheckCircle2 className="h-3 w-3 flex-shrink-0" />
              )}
              {status.message}
            </div>
          )}
        </div>
      </div>

      {/* Configured servers list */}
      <div className="space-y-2">
        <h3 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          Configured Servers ({serverEntries.length})
        </h3>

        {serverEntries.length === 0 && (
          <p className="text-xs text-muted-foreground">No MCP servers configured yet.</p>
        )}

        {serverEntries.map(([name, server]) => (
          <div
            key={name}
            className="rounded-lg border border-border p-4 space-y-2 bg-card/30"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <Server className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
                  <h4 className="text-sm font-medium truncate">{name}</h4>
                </div>
                <p className="text-xs text-muted-foreground mt-1 font-mono">
                  {server.command}
                  {server.args?.length ? ` ${server.args.join(" ")}` : ""}
                </p>
              </div>
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 w-7 p-0"
                  onClick={() => handleEdit(name)}
                  title="Edit"
                >
                  <Pencil className="h-3 w-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 w-7 p-0 text-destructive hover:text-destructive"
                  onClick={() => handleRemove(name)}
                  disabled={deleting !== null}
                  title="Remove"
                >
                  {deleting === name ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Trash2 className="h-3 w-3" />
                  )}
                </Button>
              </div>
            </div>

            {/* Env vars */}
            {server.env && Object.keys(server.env).length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(server.env).map(([key, val]) => (
                  <span
                    key={key}
                    className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-mono bg-muted text-muted-foreground"
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
