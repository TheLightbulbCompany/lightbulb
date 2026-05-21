"use client";

import { useState, useCallback, useEffect, useMemo } from "react";
import { Loader2, AlertCircle } from "lucide-react";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { ModelSelector } from "@/components/chat/ModelSelector";
import { Button } from "@/components/ui/button";
import { AgentPaperclipFields } from "@/components/control/agents/AgentPaperclipFields";
import {
  PAPERCLIP_AGENT_ROLES,
  type PaperclipMeta,
  type PaperclipRole,
} from "@/lib/paperclipMeta";
import type { AgentEntry, AgentIdentity, ConfigSnapshot } from "./agents-types";

function resolveModelPrimary(model?: string | { primary?: string; fallbacks?: string[] }): string | undefined {
  if (typeof model === "string") return model.trim() || undefined;
  if (typeof model === "object" && model) return model.primary?.trim() || undefined;
  return undefined;
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-[#8a8578]/60">{label}</div>
      <div className="text-sm font-medium truncate text-[#1a1a1a]">{value}</div>
    </div>
  );
}

export function AgentOverviewTab({ agentId, agent, onAgentUpdated }: { agentId: string; agent?: AgentEntry; onAgentUpdated?: () => void }) {
  const { data } = useGatewayRpc<Record<string, unknown>>(
    "agent.identity.get",
    { agentId },
  );
  const { data: configSnapshot, mutate: mutateConfig } = useGatewayRpc<ConfigSnapshot>("config.get");
  const callRpc = useGatewayRpcMutation();
  const [updatingModel, setUpdatingModel] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);

  const identity = (data || agent?.identity) as AgentIdentity | undefined;
  const configInner = configSnapshot?.config;

  const modelsCatalog = useMemo(
    () => configInner?.agents?.defaults?.models ?? {},
    [configInner?.agents?.defaults?.models],
  );

  const defaultModelPrimary = resolveModelPrimary(configInner?.agents?.defaults?.model);
  const agentConfigEntry = configInner?.agents?.list?.find(a => a?.id === agentId);
  const agentModelPrimary = resolveModelPrimary(agentConfigEntry?.model);
  const currentModel = agentModelPrimary || defaultModelPrimary || "";

  // Paperclip meta — role/title/capabilities. Persisted on the agent
  // entry under params.paperclip and mirrored to Paperclip via
  // _intercept_agents_update on the backend. Initial value seeded from
  // the agent's existing params.paperclip block if present.
  const existingPaperclip = useMemo(() => {
    const block = (agentConfigEntry?.params as Record<string, unknown> | undefined)?.paperclip;
    if (!block || typeof block !== "object") return undefined;
    return block as Partial<PaperclipMeta>;
  }, [agentConfigEntry]);

  const [meta, setMeta] = useState<PaperclipMeta>(() => ({
    role:
      existingPaperclip?.role &&
      PAPERCLIP_AGENT_ROLES.includes(existingPaperclip.role as PaperclipRole)
        ? (existingPaperclip.role as PaperclipRole)
        : "general",
    title: existingPaperclip?.title,
    capabilities: existingPaperclip?.capabilities,
    icon: existingPaperclip?.icon,
  }));
  const [savingMeta, setSavingMeta] = useState(false);
  const [metaError, setMetaError] = useState<string | null>(null);
  // Track whether the user has touched the meta form. Once dirty we
  // stop auto-syncing from config.get's SWR result so we don't
  // clobber in-flight edits. Reset after a successful save so a
  // future config refresh can re-seed if needed.
  const [metaDirty, setMetaDirty] = useState(false);

  // Hard reset whenever the selected agent changes. Without this,
  // the meta form's local state leaks across agents: editing Friday
  // and then switching to Tally would leave Friday's typed values
  // in the form, and clicking Save on Tally would write them to
  // Tally's row. The metaDirty guard below is intentionally bypassed
  // on agent-id change — the user crossed an agent boundary, so any
  // unsaved edits from the previous agent must NOT carry over.
  useEffect(() => {
    setMeta({
      role:
        typeof existingPaperclip?.role === "string" &&
        PAPERCLIP_AGENT_ROLES.includes(existingPaperclip.role as PaperclipRole)
          ? (existingPaperclip.role as PaperclipRole)
          : "general",
      title: existingPaperclip?.title,
      capabilities: existingPaperclip?.capabilities,
      icon: existingPaperclip?.icon,
    });
    setMetaDirty(false);
    setMetaError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  // Within an agent session, re-seed from SWR's eventual result so
  // the form picks up the agent's stored values once config.get
  // resolves. Skipped once the user starts typing — otherwise a
  // background SWR revalidate would clobber in-flight edits.
  useEffect(() => {
    if (metaDirty || !existingPaperclip) return;
    setMeta({
      role:
        typeof existingPaperclip.role === "string" &&
        PAPERCLIP_AGENT_ROLES.includes(existingPaperclip.role as PaperclipRole)
          ? (existingPaperclip.role as PaperclipRole)
          : "general",
      title: existingPaperclip.title,
      capabilities: existingPaperclip.capabilities,
      icon: existingPaperclip.icon,
    });
  }, [existingPaperclip, metaDirty]);

  // Wrap setMeta so any user interaction flips the dirty flag,
  // preventing the effect above from undoing the user's edits the
  // next time SWR revalidates.
  const setMetaFromUser = useCallback((next: PaperclipMeta) => {
    setMetaDirty(true);
    setMeta(next);
  }, []);

  const saveMeta = useCallback(async () => {
    setSavingMeta(true);
    setMetaError(null);
    try {
      // Always send every meta field — empty strings are the
      // "clear this field" sentinel that the backend's
      // apply_paperclip_meta sweeps out post-merge. Skipping empty
      // values would prevent the user from ever removing a once-set
      // title/capabilities/icon (the deep-merge would leave the old
      // value on disk).
      const block: Record<string, string> = {
        role: meta.role,
        title: meta.title?.trim() ?? "",
        capabilities: meta.capabilities?.trim() ?? "",
        icon: meta.icon?.trim() ?? "",
      };
      await callRpc("agents.update", {
        agentId,
        params: { paperclip: block },
      });
      setMetaDirty(false);
      onAgentUpdated?.();
    } catch (err) {
      setMetaError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingMeta(false);
    }
  }, [agentId, callRpc, meta, onAgentUpdated]);

  const handleModelChange = useCallback(async (newModel: string) => {
    setUpdatingModel(true);
    setModelError(null);
    try {
      await callRpc("agents.update", {
        agentId,
        ...(newModel ? { model: newModel } : { model: null }),
      });
      mutateConfig();
      onAgentUpdated?.();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error("Failed to update model:", msg);
      setModelError(msg);
    } finally {
      setUpdatingModel(false);
    }
  }, [callRpc, agentId, onAgentUpdated, mutateConfig]);

  const selectorModels = useMemo(() => {
    const models: { id: string; name: string }[] = Object.entries(modelsCatalog).map(
      ([id, entry]) => ({ id, name: entry.alias || id.split("/").pop() || id })
    );
    if (currentModel && !modelsCatalog[currentModel]) {
      models.unshift({ id: currentModel, name: currentModel.split("/").pop() || currentModel });
    }
    return models;
  }, [modelsCatalog, currentModel]);

  return (
    <div className="space-y-4 mt-2">
      <div className="rounded-lg border border-[#e0dbd0] p-4 space-y-3 bg-white">
        <h3 className="text-sm font-medium text-[#1a1a1a]">Identity</h3>
        <div className="grid grid-cols-2 gap-3">
          <InfoRow label="Agent ID" value={agentId} />
          <InfoRow label="Name" value={identity?.name || agent?.name || "\u2014"} />
          <InfoRow label="Emoji" value={identity?.emoji || "\u2014"} />
          <InfoRow label="Theme" value={identity?.theme || "\u2014"} />
        </div>
      </div>

      <div className="rounded-lg border border-[#e0dbd0] p-4 space-y-3 bg-white">
        <h3 className="text-sm font-medium text-[#1a1a1a]">Model</h3>
        {selectorModels.length > 0 ? (
          <ModelSelector
            models={selectorModels}
            selectedModel={currentModel}
            onModelChange={handleModelChange}
            disabled={updatingModel || !configSnapshot?.hash}
          />
        ) : (
          <p className="text-xs text-[#8a8578]">
            {currentModel ? currentModel.split("/").pop() : "No models configured in gateway"}
          </p>
        )}
        {updatingModel && <Loader2 className="h-3.5 w-3.5 animate-spin text-[#8a8578]" />}
        {modelError && (
          <p className="text-xs text-[#dc2626] flex items-center gap-1">
            <AlertCircle className="h-3 w-3" /> {modelError}
          </p>
        )}
      </div>

      <div className="rounded-lg border border-[#e0dbd0] p-4 space-y-3 bg-white">
        <h3 className="text-sm font-medium text-[#1a1a1a]">Role &amp; description</h3>
        <p className="text-xs text-[#8a8578]">
          How Friday sees this agent in the org chart. Used when delegating work.
        </p>

        <AgentPaperclipFields value={meta} onChange={setMetaFromUser} disabled={savingMeta} />

        {metaError && (
          <p className="text-xs text-[#dc2626] flex items-center gap-1">
            <AlertCircle className="h-3 w-3" /> {metaError}
          </p>
        )}

        <div className="flex justify-end">
          <Button size="sm" onClick={saveMeta} disabled={savingMeta}>
            {savingMeta ? <Loader2 className="h-3 w-3 animate-spin" /> : "Save"}
          </Button>
        </div>
      </div>

      {data && (
        <details className="group">
          <summary className="text-xs text-[#8a8578]/60 cursor-pointer hover:text-[#8a8578]">
            Raw identity data
          </summary>
          <pre className="mt-2 text-xs bg-[#f3efe6] rounded-lg p-3 overflow-auto max-h-48 text-[#5a5549]">
            {JSON.stringify(data, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
