"use client";

import { useState, useCallback, useMemo, useEffect } from "react";
import { Loader2, RefreshCw, Save, AlertCircle, ToggleLeft, ToggleRight } from "lucide-react";
import { useRuntimeHost } from "@lightbulb/shared";
import { useRuntimeRpc, useRuntimeRpcMutation } from "@/hooks/useRuntimeRpc";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { notifyApiError } from "@/lib/notify";
import type { AgentConfigEntry, ConfigSnapshot, ToolsCatalogResponse } from "./agents-types";

const PROFILE_POLICIES: Record<string, string[]> = {
  minimal: ["session_status"],
  coding: [
    "read", "write", "edit", "apply_patch", "exec", "process",
    "memory_search", "memory_get", "sessions_list", "sessions_history",
    "sessions_send", "sessions_spawn", "subagents", "session_status",
    "image",
  ],
  messaging: ["sessions_list", "sessions_history", "sessions_send", "session_status", "message"],
  full: [],
};

const TOOL_ALIASES: Record<string, string> = {
  bash: "exec",
  "apply-patch": "apply_patch",
};

const FALLBACK_SECTIONS: { id: string; label: string; tools: { id: string; label: string; description: string; source?: string }[] }[] = [
  { id: "files", label: "Files", tools: [
    { id: "read", label: "read", description: "Read file contents" },
    { id: "write", label: "write", description: "Create or overwrite files" },
    { id: "edit", label: "edit", description: "Edit files with search/replace" },
    { id: "apply_patch", label: "apply_patch", description: "Apply unified diffs" },
  ]},
  { id: "runtime", label: "Runtime", tools: [
    { id: "exec", label: "exec", description: "Run shell commands" },
    { id: "process", label: "process", description: "Manage background processes" },
  ]},
  { id: "web", label: "Web", tools: [
    { id: "web_search", label: "web_search", description: "Search the web" },
    { id: "web_fetch", label: "web_fetch", description: "Fetch web page content" },
  ]},
  { id: "memory", label: "Memory", tools: [
    { id: "memory_search", label: "memory_search", description: "Search agent memory" },
    { id: "memory_get", label: "memory_get", description: "Get memory entries" },
  ]},
  { id: "sessions", label: "Sessions", tools: [
    { id: "sessions_list", label: "sessions_list", description: "List active sessions" },
    { id: "sessions_history", label: "sessions_history", description: "View session history" },
    { id: "sessions_send", label: "sessions_send", description: "Send message to session" },
    { id: "sessions_spawn", label: "sessions_spawn", description: "Spawn new session" },
    { id: "session_status", label: "session_status", description: "Check session status" },
  ]},
  { id: "ui", label: "UI", tools: [
    { id: "image", label: "image", description: "Generate or process images" },
  ]},
  { id: "messaging", label: "Messaging", tools: [
    { id: "message", label: "message", description: "Send messages to channels" },
  ]},
  { id: "nodes", label: "Nodes", tools: [
    { id: "nodes_list", label: "nodes_list", description: "List compute nodes" },
    { id: "nodes_exec", label: "nodes_exec", description: "Execute on remote nodes" },
  ]},
  { id: "agents", label: "Agents", tools: [
    { id: "subagents", label: "subagents", description: "Spawn and manage sub-agents" },
  ]},
  { id: "media", label: "Media", tools: [
    { id: "audio", label: "audio", description: "Audio processing" },
    { id: "video", label: "video", description: "Video processing" },
  ]},
];

function normalizeToolId(id: string): string {
  return TOOL_ALIASES[id] ?? id;
}

function isToolAllowed(
  toolId: string,
  profileId: string,
  alsoAllow: string[],
  deny: string[],
): boolean {
  const normalized = normalizeToolId(toolId);
  if (deny.includes(normalized)) return false;
  const basePolicy = PROFILE_POLICIES[profileId];
  if (!basePolicy) return true;
  if (basePolicy.length === 0) return true;
  return basePolicy.includes(normalized) || alsoAllow.includes(normalized);
}

export function AgentToolsTab({ agentId }: { agentId: string }) {
  // Hermes gates by TOOLSET, not per tool -- there is no allow/deny list to edit, and the
  // backend now refuses `agents.update {tools}` on this runtime instead of confirming a write
  // it never performs (M6 task 10, row 12; the toolset remap is a later lane). The RPCs are
  // nulled too, not just the editor: `tools.catalog` is stubbed on the bridge, so firing it
  // would start a microVM and spin ~55s only to render an error.
  //
  // Fix round 1 (review I1): the RPCs wait for a DEFINITE "ecs", never merely "not agentcore".
  // `undefined` is "not known for this workspace" -- no status has landed, or one just switched
  // -- and firing on a guess is the one mistake with a ~55s bill attached. Same shape as
  // `useSystemHealth`'s `container && ...` hold.
  const runtimeHost = useRuntimeHost();
  const onAgentCore = runtimeHost === "agentcore";
  const rpcReady = runtimeHost === "ecs";
  const { data: catalogData, error: catalogError, isLoading: catalogLoading, mutate: mutateCatalog } =
    useRuntimeRpc<ToolsCatalogResponse>(rpcReady ? "tools.catalog" : null, { agentId, includePlugins: true });
  const { data: configSnapshot, mutate: mutateConfig } =
    useRuntimeRpc<ConfigSnapshot>(rpcReady ? "config.get" : null);
  const callRpc = useRuntimeRpcMutation();

  const [localProfile, setLocalProfile] = useState<string | null>(null);
  const [localAlsoAllow, setLocalAlsoAllow] = useState<string[]>([]);
  const [localDeny, setLocalDeny] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const configInner = configSnapshot?.config;
  const agentConfigEntry = configInner?.agents?.list?.find(
    (a: AgentConfigEntry) => a?.id === agentId,
  );
  const agentToolsConfig = agentConfigEntry?.tools as
    | { profile?: string; alsoAllow?: string[]; deny?: string[] }
    | undefined;
  const globalToolsConfig = configInner?.tools as
    | { profile?: string; allow?: string[] }
    | undefined;

  const serverAlsoAllow = agentToolsConfig?.alsoAllow ?? [];
  const serverDeny = agentToolsConfig?.deny ?? [];

  useEffect(() => {
    if (!dirty) {
      setLocalProfile(agentToolsConfig?.profile ?? null);
      setLocalAlsoAllow([...serverAlsoAllow]);
      setLocalDeny([...serverDeny]);
    }
  }, [agentToolsConfig?.profile, dirty]); // eslint-disable-line react-hooks/exhaustive-deps

  const effectiveProfile = localProfile ?? globalToolsConfig?.profile ?? "full";
  const profileSource = localProfile ? "agent" : globalToolsConfig?.profile ? "global" : "default";

  const sections = useMemo(() => {
    if (catalogData?.groups && catalogData.groups.length > 0) {
      return catalogData.groups.map((g) => ({
        id: g.id,
        label: g.label,
        source: g.source,
        tools: g.tools.map((t) => ({
          id: normalizeToolId(t.id ?? t.name),
          label: t.label ?? t.name,
          description: t.description ?? "",
          source: t.source,
        })),
      }));
    }
    if (catalogData?.tools && catalogData.tools.length > 0) {
      const grouped = new Map<string, { id: string; label: string; description: string; source?: string }[]>();
      for (const tool of catalogData.tools) {
        const group = tool.profile || tool.category || "default";
        const list = grouped.get(group) ?? [];
        list.push({
          id: normalizeToolId(tool.id ?? tool.name),
          label: tool.label ?? tool.name,
          description: tool.description ?? "",
          source: tool.source,
        });
        grouped.set(group, list);
      }
      return Array.from(grouped.entries()).map(([key, tools]) => ({
        id: key,
        label: key.charAt(0).toUpperCase() + key.slice(1),
        tools,
      }));
    }
    return FALLBACK_SECTIONS;
  }, [catalogData]);

  const allToolIds = useMemo(
    () => sections.flatMap((s) => s.tools.map((t) => t.id)),
    [sections],
  );
  const totalTools = allToolIds.length;
  const enabledCount = useMemo(
    () => allToolIds.filter((id) => isToolAllowed(id, effectiveProfile, localAlsoAllow, localDeny)).length,
    [allToolIds, effectiveProfile, localAlsoAllow, localDeny],
  );

  const toggleTool = useCallback(
    (toolId: string) => {
      const normalized = normalizeToolId(toolId);
      const currentlyAllowed = isToolAllowed(normalized, effectiveProfile, localAlsoAllow, localDeny);

      if (currentlyAllowed) {
        setLocalAlsoAllow((prev) => prev.filter((id) => id !== normalized));
        setLocalDeny((prev) => (prev.includes(normalized) ? prev : [...prev, normalized]));
      } else {
        setLocalDeny((prev) => prev.filter((id) => id !== normalized));
        const basePolicy = PROFILE_POLICIES[effectiveProfile] ?? [];
        if (basePolicy.length > 0 && !basePolicy.includes(normalized)) {
          setLocalAlsoAllow((prev) => (prev.includes(normalized) ? prev : [...prev, normalized]));
        }
      }
      setDirty(true);
    },
    [effectiveProfile, localAlsoAllow, localDeny],
  );

  const enableAll = useCallback(() => {
    setLocalAlsoAllow([...allToolIds]);
    setLocalDeny([]);
    setDirty(true);
  }, [allToolIds]);

  const disableAll = useCallback(() => {
    setLocalAlsoAllow([]);
    setLocalDeny([...allToolIds]);
    setDirty(true);
  }, [allToolIds]);

  const applyPreset = useCallback(
    (preset: string | null) => {
      setLocalProfile(preset);
      setLocalAlsoAllow([]);
      setLocalDeny([]);
      setDirty(true);
    },
    [],
  );

  const handleReload = useCallback(() => {
    setDirty(false);
    mutateCatalog();
    mutateConfig();
  }, [mutateCatalog, mutateConfig]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const toolsPayload: Record<string, unknown> = {};
      if (localProfile !== null) {
        toolsPayload.profile = localProfile;
      }
      if (localAlsoAllow.length > 0) {
        toolsPayload.alsoAllow = localAlsoAllow;
      }
      if (localDeny.length > 0) {
        toolsPayload.deny = localDeny;
      }
      await callRpc("agents.update", { agentId, tools: toolsPayload });
      setDirty(false);
      mutateConfig();
    } catch (err) {
      notifyApiError(err, "Couldn't save tool settings");
    } finally {
      setSaving(false);
    }
  }, [callRpc, agentId, localProfile, localAlsoAllow, localDeny, mutateConfig]);

  if (onAgentCore) {
    return (
      <div className="mt-4 flex items-start gap-2 px-3 py-2 rounded-lg bg-cream border border-[var(--cream-deep)]">
        <AlertCircle size={12} strokeWidth={1.75} className="text-[var(--midnight-muted)] shrink-0 mt-0.5" />
        <p className="text-xs text-[var(--midnight-muted)]">
          Per-tool access isn&apos;t available on this runtime. Tools are granted by toolset here,
          not one by one.
        </p>
      </div>
    );
  }

  // Unknown runtime: the same spinner a loading catalog shows. The editor cannot be drawn
  // honestly yet, and its RPCs are held above.
  if (catalogLoading || !rpcReady) {
    return <Loader2 size={16} className="animate-spin text-[var(--midnight-muted)] mt-4" />;
  }

  if (catalogError) {
    return (
      <div className="mt-4 space-y-2">
        <p className="text-sm text-err">{catalogError.message}</p>
        <Button variant="outline" size="sm" onClick={() => mutateCatalog()}>
          <RefreshCw size={16} className="mr-1.5" /> Retry
        </Button>
      </div>
    );
  }

  const presets = [
    { id: "minimal", label: "Minimal" },
    { id: "coding", label: "Coding" },
    { id: "messaging", label: "Messaging" },
    { id: "full", label: "Full" },
  ];

  return (
    <div className="mt-2 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-medium">Tool Access</h3>
          <p className="text-xs text-[var(--midnight-muted)]">
            {enabledCount}/{totalTools} enabled
          </p>
        </div>
        <div className="flex items-center gap-1">
          <Button variant="outline" size="sm" onClick={enableAll}>
            Enable All
          </Button>
          <Button variant="outline" size="sm" onClick={disableAll}>
            Disable All
          </Button>
          <Button variant="ghost" size="sm" onClick={handleReload}>
            <RefreshCw size={16} />
          </Button>
          <Button
            variant="default"
            size="sm"
            onClick={handleSave}
            disabled={saving || !dirty}
          >
            {saving ? (
              <Loader2 size={12} strokeWidth={1.75} className="animate-spin mr-1" />
            ) : (
              <Save size={12} strokeWidth={1.75} className="mr-1" />
            )}
            Save
          </Button>
        </div>
      </div>


      {globalToolsConfig?.allow && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-cream border border-warn">
          <AlertCircle size={12} strokeWidth={1.75} className="text-warn shrink-0" />
          <span className="text-xs text-warn">
            Global <code className="text-2xs bg-[var(--cream-deep)] px-1 rounded-sm">tools.allow</code> is set — this may restrict available tools regardless of agent config.
          </span>
        </div>
      )}

      <div className="flex items-center gap-3 text-xs text-[var(--midnight-muted)]">
        <span>
          Profile: <span className="font-medium text-[var(--midnight)]">{effectiveProfile}</span>
        </span>
        <span>
          Source: <span className="font-medium text-[var(--midnight)]">{profileSource}</span>
        </span>
        {dirty && (
          <span className="text-warn font-medium">unsaved</span>
        )}
      </div>

      <div className="flex items-center gap-1 flex-wrap">
        <span className="text-xs text-[var(--midnight-muted)] mr-1">Quick Presets:</span>
        {presets.map((preset) => (
          <Button
            key={preset.id}
            variant={effectiveProfile === preset.id && localProfile === preset.id ? "default" : "outline"}
            size="sm"
            className="h-6 text-xs px-2"
            onClick={() => applyPreset(preset.id)}
          >
            {preset.label}
          </Button>
        ))}
        <Button
          variant={localProfile === null ? "default" : "outline"}
          size="sm"
          className="h-6 text-xs px-2"
          onClick={() => applyPreset(null)}
        >
          Inherit
        </Button>
      </div>

      {sections.map((section) => (
        <div key={section.id} className="space-y-1">
          <h4 className="label-caps text-[var(--midnight-muted)]">
            {section.label}
          </h4>
          <div className="space-y-0.5">
            {section.tools.map((tool) => {
              const allowed = isToolAllowed(tool.id, effectiveProfile, localAlsoAllow, localDeny);
              return (
                <div
                  key={tool.id}
                  className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-[var(--cream-deep)] group"
                >
                  <button
                    className="shrink-0"
                    onClick={() => toggleTool(tool.id)}
                    aria-label={`Toggle ${tool.label}`}
                  >
                    {allowed ? (
                      <ToggleRight size={16} className="text-ok" />
                    ) : (
                      <ToggleLeft size={16} className="text-midnight-muted" />
                    )}
                  </button>
                  <div className="min-w-0 flex-1">
                    <div className={cn(
                      "text-sm font-medium truncate",
                      !allowed && "text-midnight-muted",
                    )}>
                      {tool.label}
                    </div>
                    {tool.description && (
                      <div className="text-xs text-midnight-muted line-clamp-1">
                        {tool.description}
                      </div>
                    )}
                  </div>
                  {tool.source && (
                    <span className="text-2xs text-midnight-muted shrink-0">
                      {String(tool.source)}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}

      <details className="group">
        <summary className="text-xs text-midnight-muted cursor-pointer hover:text-[var(--midnight-muted)]">
          Raw catalog data
        </summary>
        <pre className="mt-2 text-xs bg-[var(--cream-deep)] rounded-lg p-3 overflow-auto max-h-48 text-[var(--midnight-soft)]">
          {JSON.stringify(catalogData, null, 2)}
        </pre>
      </details>
    </div>
  );
}
