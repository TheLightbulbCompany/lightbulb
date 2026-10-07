"use client";

import { useState, useCallback, useEffect, useMemo } from "react";
import { Loader2, AlertCircle } from "lucide-react";
import { useRuntimeRpcMutation } from "@/hooks/useRuntimeRpc";
import { useModelCatalog } from "@/hooks/useConfigPatch";
import { ModelSelector } from "@/components/chat/ModelSelector";
import { buildModelNameMap, displayModelName, listCatalogModels } from "@/lib/modelDisplay";
import { Button } from "@/components/ui/button";
import { AgentPaperclipFields } from "@/components/control/agents/AgentPaperclipFields";
import {
  PAPERCLIP_AGENT_ROLES,
  PAPERCLIP_ROLE_LABELS,
  type PaperclipMeta,
  type PaperclipRole,
} from "@/lib/paperclipMeta";
import type { AgentEntry } from "./agents-types";

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="label-caps">{label}</div>
      <div className="text-sm font-medium truncate text-[var(--midnight)]">{value}</div>
    </div>
  );
}

export function AgentOverviewTab({ agentId, agent, onAgentUpdated }: { agentId: string; agent?: AgentEntry; onAgentUpdated?: () => void }) {
  // The identity, the model and the Paperclip meta all come from the roster row
  // `agents.list` already returned — `agent.identity.get` is stubbed on the
  // bridge and a Hermes config has no `agents.list`, so neither RPC is dialled.
  const { source: catalogSource } = useModelCatalog();
  const callRpc = useRuntimeRpcMutation();
  const [updatingModel, setUpdatingModel] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);

  const currentModel = agent?.model ?? "";

  // Paperclip meta — role/title/capabilities, seeded from the roster row, which
  // IS the Paperclip row. Without that the form would seed "general" for every
  // agent and Save would silently demote an engineer's role.
  const existingPaperclip = useMemo(
    () =>
      agent?.role
        ? ({ role: agent.role, title: agent.title, capabilities: agent.capabilities, icon: agent.icon } as Partial<PaperclipMeta>)
        : undefined,
    [agent?.role, agent?.title, agent?.capabilities, agent?.icon],
  );

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

  // Within an agent session, re-seed from the roster's eventual result so the
  // form picks up the agent's stored values once it resolves. Skipped once the
  // user starts typing — otherwise a background revalidate would clobber
  // in-flight edits.
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
      onAgentUpdated?.();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error("Failed to update model:", msg);
      setModelError(msg);
    } finally {
      setUpdatingModel(false);
    }
  }, [callRpc, agentId, onAgentUpdated]);

  // Display names from the owner's model list (the `models.list` RPC). Bedrock
  // ids are application-inference-profile ARNs whose last segment is an opaque
  // random suffix, so the old split("/").pop() fallback rendered garbage like
  // "ykpc0bdu467l".
  const modelNames = useMemo(() => buildModelNameMap(catalogSource), [catalogSource]);

  const selectorModels = useMemo(() => {
    const models: { id: string; name: string }[] = listCatalogModels(catalogSource).map((m) => ({
      id: m.ref,
      name: m.name,
    }));
    const seen = new Set(models.map((m) => m.id));
    if (currentModel && !seen.has(currentModel)) {
      models.unshift({
        id: currentModel,
        name: displayModelName(currentModel, modelNames) || currentModel,
      });
    }
    return models;
  }, [catalogSource, currentModel, modelNames]);

  // Hermes has no emoji/theme \u2014 identity is the Paperclip row plus SOUL.md.
  const identityRows: [string, string][] = [
    ["Agent ID", agentId],
    ["Name", agent?.name || "\u2014"],
    ["Role", PAPERCLIP_ROLE_LABELS[agent?.role as PaperclipRole] || agent?.role || "\u2014"],
    ["Title", agent?.title || "\u2014"],
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
            // The write is an `agents.update`, gated on nothing but the list —
            // there is no config hash in play (the profile is CAS'd server-side).
            disabled={updatingModel}
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
    </div>
  );
}
