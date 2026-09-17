"use client";

import { useState, useCallback, useEffect, useMemo } from "react";
import { Loader2, AlertCircle } from "lucide-react";
import { useHiddenOnAgentCore, useRuntimeHost } from "@lightbulb/shared";
import { useRuntimeRpc, useRuntimeRpcMutation } from "@/hooks/useRuntimeRpc";
import { useModelCatalog } from "@/hooks/useConfigPatch";
import { ModelSelector } from "@/components/chat/ModelSelector";
import { buildModelNameMap, displayModelName, listCatalogModels } from "@/lib/modelDisplay";
import { AgentHeartbeatCard } from "@/components/control/agents/AgentHeartbeatCard";
import { Button } from "@/components/ui/button";
import { AgentPaperclipFields } from "@/components/control/agents/AgentPaperclipFields";
import {
  PAPERCLIP_AGENT_ROLES,
  PAPERCLIP_ROLE_LABELS,
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
      <div className="label-caps">{label}</div>
      <div className="text-sm font-medium truncate text-[var(--midnight)]">{value}</div>
    </div>
  );
}

export function AgentOverviewTab({ agentId, agent, onAgentUpdated }: { agentId: string; agent?: AgentEntry; onAgentUpdated?: () => void }) {
  // M6 task 10-C (scan rows 1/8/9): on AgentCore the identity, the model and the Paperclip meta
  // all come from the roster row `agents.list` already returned. `agent.identity.get` is stubbed
  // on the bridge (a ~55s microVM start to render an error) and `config.agents.list` does not
  // exist in a Hermes config, so neither RPC is dialled there. Both are held while the runtime
  // host is unknown — guessing costs that same cold start.
  const runtimeHost = useRuntimeHost();
  const onAgentCore = runtimeHost === "agentcore";
  const rpcReady = runtimeHost === "ecs";
  // M6 task 10-D, rows 3/10: heartbeats have no Hermes equivalent (Hermes's
  // scheduler is `cron` jobs and we pin `cron.allow_agent_scheduling: false`) —
  // the card is dropped outright rather than shown inert, and its writes
  // (`config.patch` fanning out to every roster profile) never fire.
  const heartbeatVisibility = useHiddenOnAgentCore();
  const { data } = useRuntimeRpc<Record<string, unknown>>(
    rpcReady ? "agent.identity.get" : null,
    { agentId },
  );
  const { data: configSnapshot, mutate: mutateConfig } = useRuntimeRpc<ConfigSnapshot>(
    rpcReady ? "config.get" : null,
  );
  const { source: catalogSource } = useModelCatalog();
  const callRpc = useRuntimeRpcMutation();
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
  const currentModel = onAgentCore
    ? (agent?.model ?? "")
    : agentModelPrimary || defaultModelPrimary || "";

  // Paperclip meta — role/title/capabilities. Persisted on the agent
  // entry under params.paperclip and mirrored to Paperclip via
  // _intercept_agents_update on the backend. Initial value seeded from
  // the agent's existing params.paperclip block if present — or, on
  // AgentCore, from the roster row, which IS the Paperclip row. Without
  // that the form would seed "general" for every agent and Save would
  // silently demote an engineer's role.
  const existingPaperclip = useMemo(() => {
    if (onAgentCore) {
      return agent?.role
        ? ({ role: agent.role, title: agent.title, capabilities: agent.capabilities, icon: agent.icon } as Partial<PaperclipMeta>)
        : undefined;
    }
    const block = (agentConfigEntry?.params as Record<string, unknown> | undefined)?.paperclip;
    if (!block || typeof block !== "object") return undefined;
    return block as Partial<PaperclipMeta>;
  }, [agentConfigEntry, onAgentCore, agent?.role, agent?.title, agent?.capabilities, agent?.icon]);

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
      // Re-read config.get before clearing the dirty flag. The form
      // re-seeds itself from config.get's params.paperclip whenever
      // the dirty flag is false; without a fresh read it would re-seed
      // from the pre-save snapshot and the just-saved values would
      // appear to vanish on navigating back to this agent.
      await mutateConfig();
      setMetaDirty(false);
      onAgentUpdated?.();
    } catch (err) {
      setMetaError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingMeta(false);
    }
  }, [agentId, callRpc, meta, mutateConfig, onAgentUpdated]);

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

  // Display names from the owner's model list — the `models.list` RPC on AgentCore, the
  // gateway config's providers catalog on ECS. Bedrock ids are application-inference-profile
  // ARNs whose last segment is an opaque random suffix, so the old split("/").pop() fallback
  // rendered garbage like "ykpc0bdu467l".
  const modelNames = useMemo(() => buildModelNameMap(catalogSource), [catalogSource]);

  const selectorModels = useMemo(() => {
    // Primary source: the owner's model list (see above).
    // agents.defaults.models is a per-deployment alias map that provision
    // does NOT write — kept as a secondary source for hand-edited ECS configs.
    const models: { id: string; name: string }[] = listCatalogModels(catalogSource).map((m) => ({
      id: m.ref,
      name: m.name,
    }));
    const seen = new Set(models.map((m) => m.id));
    for (const [id, entry] of Object.entries(modelsCatalog)) {
      if (seen.has(id)) continue;
      models.push({ id, name: entry.alias || displayModelName(id, modelNames) || id });
      seen.add(id);
    }
    if (currentModel && !seen.has(currentModel)) {
      models.unshift({
        id: currentModel,
        name: displayModelName(currentModel, modelNames) || currentModel,
      });
    }
    return models;
  }, [catalogSource, modelsCatalog, currentModel, modelNames]);

  // Hermes has no emoji/theme \u2014 identity there is the Paperclip row plus SOUL.md, so the two
  // rows that would always read "\u2014" are replaced by the two that carry the answer (row 8).
  const identityRows: [string, string][] = onAgentCore
    ? [
        ["Agent ID", agentId],
        ["Name", agent?.name || "\u2014"],
        ["Role", PAPERCLIP_ROLE_LABELS[agent?.role as PaperclipRole] || agent?.role || "\u2014"],
        ["Title", agent?.title || "\u2014"],
      ]
    : [
        ["Agent ID", agentId],
        ["Name", identity?.name || agent?.name || "\u2014"],
        ["Emoji", identity?.emoji || "\u2014"],
        ["Theme", identity?.theme || "\u2014"],
      ];

  return (
    <div className="space-y-4 mt-2">
      <div className="rounded-lg border border-[var(--edge)] p-4 space-y-3 bg-cream">
        <h3 className="text-sm font-medium text-[var(--midnight)]">Identity</h3>
        <div className="grid grid-cols-2 gap-3">
          {identityRows.map(([label, value]) => (
            <InfoRow key={label} label={label} value={value} />
          ))}
        </div>
      </div>

      <div className="rounded-lg border border-[var(--edge)] p-4 space-y-3 bg-cream">
        <h3 className="text-sm font-medium text-[var(--midnight)]">Model</h3>
        {selectorModels.length > 0 ? (
          <ModelSelector
            models={selectorModels}
            selectedModel={currentModel}
            onModelChange={handleModelChange}
            // On AgentCore the write is an `agents.update`, gated on nothing but the list —
            // there is no config hash in play (the profile is CAS'd server-side).
            disabled={updatingModel || (!onAgentCore && !configSnapshot?.hash)}
          />
        ) : (
          <p className="text-xs text-[var(--midnight-muted)]">
            {currentModel
              ? displayModelName(currentModel, modelNames) || "Custom model"
              : "No models available for this workspace"}
          </p>
        )}
        {updatingModel && <Loader2 size={16} className="animate-spin text-[var(--midnight-muted)]" />}
        {modelError && (
          <p className="text-xs text-err flex items-center gap-1">
            <AlertCircle size={12} strokeWidth={1.75} /> {modelError}
          </p>
        )}
      </div>

      {heartbeatVisibility !== "hidden" && <AgentHeartbeatCard agentId={agentId} />}

      <div className="rounded-lg border border-[var(--edge)] p-4 space-y-3 bg-cream">
        <h3 className="text-sm font-medium text-[var(--midnight)]">Role &amp; description</h3>
        <p className="text-xs text-[var(--midnight-muted)]">
          How Friday sees this agent in the org chart. Used when delegating work.
        </p>

        <AgentPaperclipFields value={meta} onChange={setMetaFromUser} disabled={savingMeta} />

        {metaError && (
          <p className="text-xs text-err flex items-center gap-1">
            <AlertCircle size={12} strokeWidth={1.75} /> {metaError}
          </p>
        )}

        <div className="flex justify-end">
          <Button size="sm" onClick={saveMeta} disabled={savingMeta}>
            {savingMeta ? <Loader2 size={12} strokeWidth={1.75} className="animate-spin" /> : "Save"}
          </Button>
        </div>
      </div>

      {data && (
        <details className="group">
          <summary className="text-xs text-midnight-muted cursor-pointer hover:text-[var(--midnight-muted)]">
            Raw identity data
          </summary>
          <pre className="mt-2 text-xs bg-[var(--cream-deep)] rounded-lg p-3 overflow-auto max-h-48 text-[var(--midnight-soft)]">
            {JSON.stringify(data, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
