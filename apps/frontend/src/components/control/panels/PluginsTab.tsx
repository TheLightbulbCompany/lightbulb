"use client";

import { useState } from "react";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { Loader2, RefreshCw, Package } from "lucide-react";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { capture } from "@/lib/analytics";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ConfigSnapshot, ToolsCatalogResponse } from "./agents-types";

// Plugins Isol8 manages itself — provider plumbing + bundled workflow
// extensions. Disabling them can break inference or the agent runtime, so
// they're shown read-only (mirrors how the Skills tab locks `always` skills).
const LOCKED_PLUGIN_IDS = new Set(["amazon-bedrock", "lobster", "llm-task"]);

// Channel plugins are enabled/disabled through the Channels panel (channel
// enable = hot per-channel restart). Listing them here as generic switches
// invites breaking a live channel from the wrong surface.
const CHANNEL_PLUGIN_IDS = new Set(["slack", "discord", "telegram", "whatsapp", "googlechat", "feishu", "signal"]);

interface PluginEntryConfig {
  enabled?: boolean;
  [key: string]: unknown;
}

type PluginKind = "provider" | "workflow" | "plugin";

interface PluginRow {
  id: string;
  enabled: boolean;
  locked: boolean;
  kind: PluginKind;
  toolCount: number;
}

function inferKind(id: string): PluginKind {
  if (id === "lobster" || id === "llm-task") return "workflow";
  if (id.includes("bedrock") || id.includes("openai") || id.includes("anthropic")) return "provider";
  return "plugin";
}

export function PluginsTab() {
  const canManage = useIsWorkspaceAdmin();

  const { data: snapshot, error, isLoading, mutate } = useGatewayRpc<ConfigSnapshot>("config.get");
  const callRpc = useGatewayRpcMutation();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);

  const config = snapshot?.config;
  const entries =
    (config?.plugins as { entries?: Record<string, PluginEntryConfig> } | undefined)?.entries ?? {};

  // tools.catalog needs an agentId; reuse the first configured agent. Skip the
  // fetch (method=null) until an agent exists — tool counts are pure enrichment,
  // not load-bearing, so it's fine for them to populate a beat later.
  const defaultAgentId = config?.agents?.list?.[0]?.id;
  const { data: catalog } = useGatewayRpc<ToolsCatalogResponse>(
    defaultAgentId ? "tools.catalog" : null,
    { agentId: defaultAgentId, includePlugins: true },
  );

  const pluginTools = (catalog?.groups ?? [])
    .flatMap((g) => g.tools ?? [])
    .concat(catalog?.tools ?? []);
  const toolCounts: Record<string, number> = {};
  for (const t of pluginTools) {
    if (t.source === "plugin" && t.pluginId) {
      toolCounts[t.pluginId] = (toolCounts[t.pluginId] ?? 0) + 1;
    }
  }

  const ids = Array.from(new Set([...Object.keys(entries), ...Object.keys(toolCounts)]))
    .filter((id) => !CHANNEL_PLUGIN_IDS.has(id))
    .sort();
  const rows: PluginRow[] = ids.map((id) => ({
    id,
    // Enabled unless the entry explicitly sets enabled:false.
    enabled: entries[id]?.enabled !== false,
    locked: LOCKED_PLUGIN_IDS.has(id),
    kind: inferKind(id),
    toolCount: toolCounts[id] ?? 0,
  }));

  const handleToggle = async (row: PluginRow) => {
    setBusyId(row.id);
    setToggleError(null);
    try {
      await callRpc("config.patch", {
        raw: JSON.stringify({ plugins: { entries: { [row.id]: { enabled: !row.enabled } } } }),
        // config.patch is optimistic-concurrency: the gateway requires the
        // hash of the config snapshot we read and rejects the write if the
        // file changed underneath us. Send the hash from our config.get read.
        baseHash: snapshot?.hash,
      });
      capture("plugin_toggled", { plugin: row.id, enabled: !row.enabled });
      await mutate();
    } catch (err) {
      console.error("Failed to toggle plugin:", err);
      setToggleError(err instanceof Error ? err.message : "Failed to update plugin.");
      // Refresh so a retry carries the latest base hash (handles a
      // "config changed since last load" rejection self-healingly).
      await mutate();
    } finally {
      setBusyId(null);
    }
  };

  if (isLoading) {
    return (
      <div className="flex-1 flex items-center justify-center py-20">
        <Loader2 size={20} className="animate-spin text-[var(--midnight-muted)]" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6 space-y-3">
        <p className="text-sm text-destructive">{error.message}</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw size={16} className="mr-1.5" /> Retry
        </Button>
      </div>
    );
  }

  return (
    <div className="bg-[var(--cream)] min-h-full">
      <div className="p-6 space-y-5">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="font-editorial-serif text-title">
              Plugins
            </h2>
            <p className="text-sm text-[var(--midnight-muted)] mt-0.5">
              OpenClaw plugins loaded in your container. A plugin bundles tools, skills,
              providers and channels. Lightbulb-managed plugins are locked.
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={() => mutate()} className="text-[var(--midnight-muted)] hover:text-[var(--midnight)]">
            <RefreshCw size={16} />
          </Button>
        </div>

        {toggleError && (
          <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-cream border border-err">
            <span className="text-xs text-err">{toggleError}</span>
          </div>
        )}

        {rows.length === 0 && (
          <p className="text-sm text-[var(--midnight-muted)] text-center py-8">
            No plugins configured.
          </p>
        )}

        <div className="grid gap-3" data-testid="plugins-list">
          {rows.map((row) => (
            <div key={row.id} className="rounded-xl border border-[var(--edge)] bg-cream p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-start gap-3 min-w-0 flex-1">
                  <Package size={20} className="text-[var(--midnight-muted)] flex-shrink-0 mt-0.5" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h3
                        className="text-sm font-semibold text-[var(--midnight)] truncate"
                      >
                        {row.id}
                      </h3>
                      <span className="inline-flex items-center px-2 py-0.5 rounded-full text-2xs font-medium bg-cream-deep text-[var(--midnight-muted)] flex-shrink-0">
                        {row.kind}
                      </span>
                      {row.locked && (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-2xs font-medium bg-cream-deep text-[var(--midnight-muted)] flex-shrink-0">
                          Managed
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                      {row.toolCount > 0
                        ? `Provides ${row.toolCount} tool${row.toolCount === 1 ? "" : "s"}`
                        : "No tools contributed"}
                    </p>
                  </div>
                </div>

                <div className="flex items-center gap-2 flex-shrink-0">
                  <span className={cn("text-xs font-medium", row.enabled ? "text-ok" : "text-[var(--midnight-muted)]")}>
                    {row.enabled ? "Enabled" : "Disabled"}
                  </span>
                  {!row.locked && canManage && (
                    <button
                      onClick={() => handleToggle(row)}
                      disabled={busyId !== null}
                      className="px-3 py-1.5 rounded-lg text-xs font-medium border border-[var(--edge)] text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:border-[var(--midnight-faint)] transition-colors disabled:opacity-50"
                    >
                      {busyId === row.id ? (
                        <Loader2 size={12} strokeWidth={1.75} className="animate-spin" />
                      ) : row.enabled ? (
                        "Disable"
                      ) : (
                        "Enable"
                      )}
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>

        <p className="text-xs text-[var(--midnight-muted)]">
          Channel plugins (Slack, Discord, Telegram…) are managed in the Channels panel.
        </p>
      </div>
    </div>
  );
}
