"use client";

import { useState, useCallback } from "react";
import { Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/button";

import { capture } from "@/lib/analytics";
import { normalizeAgentId, validateAgentName } from "@/lib/agentIdentity";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { useBilling } from "@/hooks/useBilling";
import { AgentPaperclipFields } from "@/components/control/agents/AgentPaperclipFields";
import type { PaperclipMeta } from "@/lib/paperclipMeta";

interface AgentCreateFormProps {
  existingNames: string[];
  onCreated: () => void;
  onCancel: () => void;
}

export function AgentCreateForm({ existingNames, onCreated, onCancel }: AgentCreateFormProps) {
  const [name, setName] = useState("");
  const [meta, setMeta] = useState<PaperclipMeta>({ role: "general" });
  const [error, setError] = useState<string | null>(null);
  // Set only for the plan agent-limit refusal, so the form can offer the
  // upgrade instead of rendering an error the user can do nothing about.
  const [atAgentLimit, setAtAgentLimit] = useState(false);
  const [creating, setCreating] = useState(false);
  const [upgrading, setUpgrading] = useState(false);
  const [upgradeError, setUpgradeError] = useState<string | null>(null);
  const callRpc = useGatewayRpcMutation();
  const { upgradeToUltra } = useBilling();

  // Mirrors UltraUpgradeRow in app/settings/page.tsx. Swallowing a failure
  // matters more here than there: this button is the ONLY way past the wall the
  // user just hit, so an inert click with no feedback strands them.
  const handleUpgrade = useCallback(async () => {
    capture("billing_ultra_upgrade_clicked", { source: "agent_limit" });
    setUpgradeError(null);
    setUpgrading(true);
    try {
      // The fresh-checkout path redirects away and never returns; the in-place
      // swap resolves here. Clear the spinner either way.
      await upgradeToUltra();
    } catch {
      setUpgradeError("Couldn't start the upgrade. Please try again.");
    } finally {
      setUpgrading(false);
    }
  }, [upgradeToUltra]);

  const normalizedId = normalizeAgentId(name);
  const clientError = validateAgentName(name, existingNames);
  const canCreate = name.trim() !== "" && clientError === null && !creating;

  const handleCreate = useCallback(async () => {
    if (!canCreate) return;
    setCreating(true);
    setError(null);
    setAtAgentLimit(false);
    try {
      // Build params.paperclip payload — only include keys with non-empty values
      // so the backend's paperclip_meta.read_from_agent_entry doesn't have to
      // sanitize whitespace-only values into None.
      const paperclipBlock: Record<string, string> = { role: meta.role };
      if (meta.title && meta.title.trim()) paperclipBlock.title = meta.title.trim();
      if (meta.capabilities && meta.capabilities.trim())
        paperclipBlock.capabilities = meta.capabilities.trim();
      if (meta.icon && meta.icon.trim()) paperclipBlock.icon = meta.icon.trim();

      await callRpc(
        "agents.create",
        {
          name: name.trim(),
          // OpenClaw's agents.create requires a non-empty `workspace` string —
          // it does NOT inherit agents.defaults.workspace at create time
          // (only at runtime via resolveAgentWorkspaceDir). Pass the same
          // path the default would resolve to, so the agent lands at
          // .openclaw/workspace-{id}/ on EFS — matching main's explicit
          // workspace-main override.
          workspace: ".openclaw/workspace-" + normalizedId,
          reasoningDefault: "stream",
          params: { paperclip: paperclipBlock },
        },
        // The server-side create chain (Paperclip + container RPCs + device
        // pairing) routinely exceeds the generic 30s budget while still
        // succeeding; 90s stops a successful create from surfacing as a
        // timeout. Mirrors useAgents.createAgent.
        90_000,
      );
      capture("agent_created", { agent_name: name.trim(), paperclip_role: meta.role });
      onCreated();
    } catch (err) {
      // The backend answers an over-budget create with a structured gateway
      // error; GatewayRpcError keeps `code` rather than flattening it into the
      // message, so match on the code and let the message stay the human
      // sentence the backend wrote.
      setAtAgentLimit((err as { code?: string } | null)?.code === "agent_budget_exceeded");
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }, [canCreate, callRpc, name, normalizedId, meta, onCreated]);

  return (
    <div className="rounded-lg border border-border bg-card/30 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">New Agent</h3>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md p-1 text-muted-foreground hover:text-foreground hover:bg-[#f0ebe2] transition-colors"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setError(null);
            // Clear the limit state with the error it belongs to. The upgrade
            // card renders on this flag alone, so leaving it set would keep an
            // upgrade prompt (above a now-empty message) on screen while the
            // user types a different name.
            setAtAgentLimit(false);
          }}
          placeholder="e.g. Research Assistant"
          disabled={creating}
          onKeyDown={(e) => {
            if (e.key === "Escape") onCancel();
          }}
          autoFocus
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
      </div>

      <AgentPaperclipFields value={meta} onChange={setMeta} disabled={creating} />

      {(clientError || error) && !atAgentLimit && (
        <p className="text-xs text-red-500">{clientError || error}</p>
      )}
      {/* The plan agent limit is not a failure the user can retry their way out
          of, so it gets an action instead of red text. Same reasoning as the
          marketplace hire's missing_credentials branch: "try again" copy on a
          wall the user cannot clear just sends them back into it. */}
      {atAgentLimit && (
        <div className="rounded-md border border-border bg-muted/40 p-3 space-y-2">
          <p className="text-xs text-muted-foreground">{error}</p>
          <Button type="button" size="sm" disabled={upgrading} onClick={handleUpgrade}>
            {upgrading ? (
              <>
                <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                Starting upgrade…
              </>
            ) : (
              "Upgrade to Ultra Pro"
            )}
          </Button>
          {upgradeError && <p className="text-xs text-red-500">{upgradeError}</p>}
        </div>
      )}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={creating}>
          Cancel
        </Button>
        <Button size="sm" onClick={handleCreate} disabled={!canCreate}>
          {creating ? <Loader2 className="h-3 w-3 animate-spin" /> : "Create"}
        </Button>
      </div>
    </div>
  );
}
