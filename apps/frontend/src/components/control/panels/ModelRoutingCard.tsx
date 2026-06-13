"use client";

import { useMemo, useState } from "react";
import { Loader2, AlertCircle } from "lucide-react";
import { useConfigPatch } from "@/hooks/useConfigPatch";
import { listCatalogModels } from "@/lib/modelDisplay";
import {
  buildDefaultModelPatch,
  buildOverheadModelPatch,
  readDefaultModelPrimary,
  readOverheadModel,
} from "@/lib/modelRouting";

type ProviderChoice = "chatgpt_oauth" | "byo_key" | "bedrock_claude";

const CARD = "rounded-lg border border-[#e0dbd0] bg-white p-4 space-y-3";
const EYEBROW = "text-[10px] uppercase tracking-wider text-[#8a8578]/60";
const SELECT_CLS =
  "rounded-md border border-[#e0dbd0] bg-white px-2 py-1.5 text-sm text-[#1a1a1a] disabled:opacity-50 w-full";

/**
 * Owner-level model routing: the default model every agent inherits, and
 * the "overhead" model that pure-maintenance work runs on (history
 * compaction, memory flush, default heartbeat). Provisioned baseline:
 * default = Sonnet (so a runaway loop never pays top-tier rates per turn),
 * overhead = Haiku, with only the CEO agent pinned to Opus per-agent.
 * Writes go through config.patch and may briefly restart the gateway.
 */
export function ModelRoutingCard({ providerChoice }: { providerChoice?: ProviderChoice | null }) {
  const { config, configReady, patchConfig } = useConfigPatch();
  const catalogModels = useMemo(() => listCatalogModels(config), [config]);
  const currentDefault = readDefaultModelPrimary(config);
  const currentOverhead = readOverheadModel(config);

  const [saving, setSaving] = useState<"default" | "overhead" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [restartNotice, setRestartNotice] = useState(false);

  // ChatGPT is a flat-rate plan with a fixed model set that isn't part of the
  // routable catalog (the codex provider uses bundled defaults, so
  // models.providers is empty). Gate on the provider — which updates the
  // moment the switch commits — rather than the lagging config catalog, so
  // we never flash the old Bedrock dropdowns after a switch. Show what it
  // runs on instead of vanishing silently.
  if (providerChoice === "chatgpt_oauth") {
    return (
      <div className={CARD}>
        <span className={EYEBROW}>MODEL ROUTING</span>
        <p className="text-sm text-[#1a1a1a]">Running on your ChatGPT plan.</p>
        <div className="grid grid-cols-2 gap-4 text-xs text-[#8a8578]">
          <div className="space-y-0.5">
            <div className="text-sm text-[#1a1a1a]">Agents — GPT-5.5</div>
            <span>What agents use by default.</span>
          </div>
          <div className="space-y-0.5">
            <div className="text-sm text-[#1a1a1a]">Maintenance — GPT-5.4 mini</div>
            <span>Compaction, memory flush, heartbeats.</span>
          </div>
        </div>
        <p className="text-xs text-[#8a8578]">
          Model routing is managed automatically on the ChatGPT plan.
        </p>
      </div>
    );
  }

  // No catalog (BYO containers without a providers list) → nothing to route
  // between; hide rather than show empty dropdowns.
  if (catalogModels.length === 0) return null;

  const apply = async (kind: "default" | "overhead", ref: string) => {
    if (!ref) return;
    setSaving(kind);
    setError(null);
    setRestartNotice(false);
    try {
      const res = await patchConfig(
        kind === "default" ? buildDefaultModelPatch(ref) : buildOverheadModelPatch(ref),
      );
      if (res?.restart) setRestartNotice(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(null);
    }
  };

  return (
    <div className={CARD}>
      <span className={EYEBROW}>MODEL ROUTING</span>

      <div className="grid grid-cols-2 gap-4">
        <label className="flex flex-col gap-1 text-xs text-[#8a8578]">
          Default model
          <select
            className={SELECT_CLS}
            value={currentDefault ?? ""}
            disabled={!configReady || saving !== null}
            onChange={(e) => void apply("default", e.target.value)}
          >
            {!currentDefault && <option value="">Select…</option>}
            {catalogModels.map((m) => (
              <option key={m.ref} value={m.ref}>
                {m.name}
              </option>
            ))}
          </select>
          <span>What agents use unless overridden per-agent.</span>
        </label>

        <label className="flex flex-col gap-1 text-xs text-[#8a8578]">
          Overhead model
          <select
            className={SELECT_CLS}
            value={currentOverhead ?? ""}
            disabled={!configReady || saving !== null}
            onChange={(e) => void apply("overhead", e.target.value)}
          >
            {!currentOverhead && <option value="">Select…</option>}
            {catalogModels.map((m) => (
              <option key={m.ref} value={m.ref}>
                {m.name}
              </option>
            ))}
          </select>
          <span>
            Maintenance work — compaction, memory flush, heartbeats. Cheapest
            tier recommended.
          </span>
        </label>
      </div>

      {saving && <Loader2 className="h-3.5 w-3.5 animate-spin text-[#8a8578]" />}
      {error && (
        <p className="text-xs text-[#dc2626] flex items-center gap-1">
          <AlertCircle className="h-3 w-3" /> {error}
        </p>
      )}
      {restartNotice && (
        <p className="text-xs text-[#8a8578]">
          Applied — the agent gateway is restarting briefly; chat reconnects
          automatically.
        </p>
      )}
    </div>
  );
}
