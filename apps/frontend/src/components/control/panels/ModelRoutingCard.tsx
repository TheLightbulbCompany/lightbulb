"use client";

import { useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useConfigPatch } from "@/hooks/useConfigPatch";
import { notifyApiError } from "@/lib/notify";
import { listCatalogModels } from "@/lib/modelDisplay";
import {
  buildDefaultModelPatch,
  buildOverheadModelPatch,
  readDefaultModelPrimary,
  readOverheadModel,
} from "@/lib/modelRouting";

import type { ProviderChoiceId as ProviderChoice } from "@/lib/providerCards";

const CARD = "rounded-lg border border-[var(--edge)] bg-cream p-4 space-y-3";
const EYEBROW = "label-caps";
const SELECT_CLS =
  "rounded-md border border-[var(--edge)] bg-cream-deep px-2 py-1.5 text-sm text-[var(--midnight)] disabled:opacity-50 w-full";

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
        <p className="text-sm text-[var(--midnight)]">Running on your ChatGPT plan.</p>
        <div className="grid grid-cols-2 gap-4 text-xs text-[var(--midnight-muted)]">
          <div className="space-y-0.5">
            <div className="text-sm text-[var(--midnight)]">Agents — GPT-5.5</div>
            <span>What agents use by default.</span>
          </div>
          <div className="space-y-0.5">
            <div className="text-sm text-[var(--midnight)]">Maintenance — GPT-5.4 mini</div>
            <span>Compaction, memory flush, heartbeats.</span>
          </div>
        </div>
        <p className="text-xs text-[var(--midnight-muted)]">
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
    setRestartNotice(false);
    try {
      const res = await patchConfig(
        kind === "default" ? buildDefaultModelPatch(ref) : buildOverheadModelPatch(ref),
      );
      if (res?.restart) setRestartNotice(true);
    } catch (err) {
      notifyApiError(err, "Couldn't update model routing");
    } finally {
      setSaving(null);
    }
  };

  return (
    <div className={CARD}>
      <span className={EYEBROW}>MODEL ROUTING</span>

      <div className="grid grid-cols-2 gap-4">
        <label className="flex flex-col gap-1 text-xs text-[var(--midnight-muted)]">
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

        <label className="flex flex-col gap-1 text-xs text-[var(--midnight-muted)]">
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

      {saving && <Loader2 size={16} className="animate-spin text-[var(--midnight-muted)]" />}
      {restartNotice && (
        <p className="text-xs text-[var(--midnight-muted)]">
          Applied — the agent gateway is restarting briefly; chat reconnects
          automatically.
        </p>
      )}
    </div>
  );
}
