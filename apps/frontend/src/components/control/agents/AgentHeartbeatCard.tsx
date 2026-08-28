"use client";

import { useEffect, useMemo, useState } from "react";
import { Loader2, AlertCircle } from "lucide-react";
import { useConfigPatch } from "@/hooks/useConfigPatch";
import { listCatalogModels } from "@/lib/modelDisplay";
import {
  HEARTBEAT_CADENCES,
  buildAgentHeartbeatPatch,
  readAgentHeartbeat,
  readDefaultHeartbeat,
  type HeartbeatSettings,
} from "@/lib/modelRouting";

const SELECT_CLS =
  "rounded-md border border-[var(--edge)] bg-white px-2 py-1.5 text-sm text-[var(--midnight)] disabled:opacity-50";
const TIME_CLS =
  "rounded-md border border-[var(--edge)] bg-white px-2 py-1 text-sm text-[var(--midnight)] disabled:opacity-50";

/**
 * Per-agent heartbeat controls: cadence, model, active hours.
 *
 * Heartbeats are autonomous runs (the agent wakes, reads HEARTBEAT.md, does
 * maintenance) — they cost real tokens around the clock, which is why each
 * knob exists: cadence bounds how often, the model override routes them to
 * a cheaper tier, and active hours stop them entirely outside a window.
 * Unset fields inherit `agents.defaults.heartbeat` (provisioned: every 30m
 * on the cheap tier). Writes go through config.patch, which may briefly
 * restart the agent gateway.
 */
export function AgentHeartbeatCard({ agentId }: { agentId: string }) {
  const { config, configReady, patchConfig } = useConfigPatch();

  const defaults = useMemo(() => readDefaultHeartbeat(config), [config]);
  const stored = useMemo(() => readAgentHeartbeat(config, agentId), [config, agentId]);
  const catalogModels = useMemo(() => listCatalogModels(config), [config]);

  // "" = inherit for both selects.
  const [every, setEvery] = useState<string>("");
  const [model, setModel] = useState<string>("");
  const [hoursEnabled, setHoursEnabled] = useState(false);
  const [hoursStart, setHoursStart] = useState("09:00");
  const [hoursEnd, setHoursEnd] = useState("18:00");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [restartNotice, setRestartNotice] = useState(false);

  // Seed from the stored override; re-seed on agent switch or when a config
  // refresh lands while the form is untouched (same pattern as the
  // Paperclip-meta form above this card).
  useEffect(() => {
    setDirty(false);
    setError(null);
    setRestartNotice(false);
  }, [agentId]);

  useEffect(() => {
    if (dirty) return;
    setEvery(stored.every ?? "");
    setModel(stored.model ?? "");
    setHoursEnabled(Boolean(stored.activeHours));
    if (stored.activeHours) {
      setHoursStart(stored.activeHours.start);
      setHoursEnd(stored.activeHours.end);
    } else {
      // Reset to the placeholder window — without this, switching from an
      // agent WITH active hours to one without leaks the previous agent's
      // times into this form, and enabling the checkbox would save them.
      setHoursStart("09:00");
      setHoursEnd("18:00");
    }
  }, [stored, dirty]);

  const save = async () => {
    setSaving(true);
    setError(null);
    setRestartNotice(false);
    try {
      const settings: HeartbeatSettings = {
        ...(every ? { every } : {}),
        ...(model ? { model } : {}),
        ...(hoursEnabled ? { activeHours: { start: hoursStart, end: hoursEnd } } : {}),
      };
      const res = await patchConfig(buildAgentHeartbeatPatch(agentId, settings));
      if (res?.restart) setRestartNotice(true);
      setDirty(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const markDirty = () => setDirty(true);
  const defaultCadence = defaults.every ?? "30m";
  const defaultModelName =
    catalogModels.find((m) => m.ref === defaults.model)?.name ?? "default model";

  return (
    <div className="rounded-lg border border-[var(--edge)] p-4 space-y-3 bg-white">
      <h3 className="text-sm font-medium text-[var(--midnight)]">Heartbeat</h3>
      <p className="text-xs text-[var(--midnight-muted)]">
        Periodic autonomous check-ins. These run (and cost tokens) even when
        you&apos;re away — tune the cadence, route them to a cheaper model, or
        restrict them to working hours.
      </p>

      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 text-xs text-[var(--midnight-muted)]">
          Cadence
          <select
            className={SELECT_CLS}
            value={every}
            disabled={saving || !configReady}
            onChange={(e) => {
              setEvery(e.target.value);
              markDirty();
            }}
          >
            <option value="">{`Inherit (every ${defaultCadence})`}</option>
            {HEARTBEAT_CADENCES.map((c) => (
              <option key={c} value={c}>
                every {c}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs text-[var(--midnight-muted)]">
          Model
          <select
            className={SELECT_CLS}
            value={model}
            disabled={saving || !configReady}
            onChange={(e) => {
              setModel(e.target.value);
              markDirty();
            }}
          >
            <option value="">{`Inherit (${defaultModelName})`}</option>
            {catalogModels.map((m) => (
              <option key={m.ref} value={m.ref}>
                {m.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="space-y-2">
        <label className="flex items-center gap-2 text-xs text-[var(--midnight-muted)]">
          <input
            type="checkbox"
            checked={hoursEnabled}
            disabled={saving || !configReady}
            onChange={(e) => {
              setHoursEnabled(e.target.checked);
              markDirty();
            }}
          />
          Only during active hours
        </label>
        {hoursEnabled && (
          <div className="flex items-center gap-2 text-xs text-[var(--midnight-muted)]">
            <input
              type="time"
              className={TIME_CLS}
              value={hoursStart}
              disabled={saving}
              onChange={(e) => {
                setHoursStart(e.target.value);
                markDirty();
              }}
            />
            <span>to</span>
            <input
              type="time"
              className={TIME_CLS}
              value={hoursEnd}
              disabled={saving}
              onChange={(e) => {
                setHoursEnd(e.target.value);
                markDirty();
              }}
            />
          </div>
        )}
      </div>

      {error && (
        <p className="text-xs text-[#dc2626] flex items-center gap-1">
          <AlertCircle className="h-3 w-3" /> {error}
        </p>
      )}
      {restartNotice && (
        <p className="text-xs text-[var(--midnight-muted)]">
          Applied — the agent gateway is restarting briefly; chat reconnects
          automatically.
        </p>
      )}

      <button
        type="button"
        onClick={save}
        disabled={!dirty || saving || !configReady}
        className="rounded-full bg-[var(--midnight)] hover:bg-[#0a5c3e] text-white px-4 py-1.5 text-sm disabled:opacity-50 inline-flex items-center gap-2"
      >
        {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
        Save heartbeat settings
      </button>
    </div>
  );
}
