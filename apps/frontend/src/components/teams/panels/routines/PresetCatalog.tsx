"use client";

// "Pick one to switch on." Rendered ABOVE the routines table by RoutinesPanel.
//
// One row per preset: name, one-sentence summary, cadence, `Needs setup` when
// a dependency is unconnected, and a switch. Switching one on runs the same
// two calls the builder's schedule rung makes — see presets.ts — and the
// result is an ordinary Paperclip routine in the table below. Nothing here
// touches the builder, the webhook rungs, or the connected-app trigger flow.
//
// Presets already switched on are hidden from the catalog rather than shown
// as "on": once created, the routine lives in the table with full controls
// (pause, run, delete, detail), and a second toggle for the same thing would
// be a worse copy of what the table already offers.

import { useState } from "react";
import { Plug } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useConnections } from "@/hooks/useConnections";
import { notifyApiError, notifySuccess } from "@/lib/notify";
import { handleShellLinkClick } from "@/lib/shellNav";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { ROUTINE_PRESETS, presetIsOn, type RoutinePreset } from "./presets";

const INTEGRATIONS = "/chat?panel=integrations";

/** The agent a preset is assigned to: the CEO if there is one, else the first
 *  agent still running. Same default the builder's assignee picker lands on. */
function defaultAssignee(agents: CompanyAgent[]): string | undefined {
  const live = agents.filter((a) => a.status !== "terminated");
  return (live.find((a) => a.role === "ceo") ?? live[0])?.id;
}

function PresetSwitch({
  disabled,
  busy,
  label,
  onSwitchOn,
}: {
  disabled: boolean;
  busy: boolean;
  label: string;
  onSwitchOn: () => void;
}) {
  // Same styled role="switch" button RoutinesPanel uses for its enable
  // toggle — a preset is off until switched on, so it renders unchecked.
  return (
    <button
      type="button"
      role="switch"
      aria-checked={false}
      aria-label={`Switch on ${label}`}
      disabled={disabled || busy}
      onClick={onSwitchOn}
      className="relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50"
      style={{ backgroundColor: "var(--cream-deep)", borderColor: "var(--edge)" }}
    >
      <span
        aria-hidden="true"
        className="inline-block h-3.5 w-3.5 translate-x-0.5 transform rounded-full bg-cream shadow-sm"
      />
    </button>
  );
}

export function PresetCatalog({
  agents,
  routineTitles,
  onCreated,
}: {
  agents: CompanyAgent[];
  /** Titles of every existing routine, to hide presets already switched on. */
  routineTitles: Set<string>;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const { connections } = useConnections();
  const [busy, setBusy] = useState<string | null>(null);

  const connected = new Set(connections.filter((c) => c.status === "connected").map((c) => c.provider));
  const remaining = ROUTINE_PRESETS.filter((p) => !presetIsOn(p, routineTitles));
  if (!remaining.length) return null;

  const assignee = defaultAssignee(agents);

  async function switchOn(preset: RoutinePreset) {
    setBusy(preset.key);
    try {
      // Identical to CreateRoutineDialog's schedule rung — two calls, the
      // trigger attached after, timezone from the browser as upstream does.
      const routine = await post<{ id: string }>("/routines", {
        title: preset.title,
        description: preset.description,
        assignee_agent_id: assignee,
        status: "active",
      });
      await post(`/routines/${routine.id}/triggers`, {
        kind: "schedule",
        cron_expression: preset.cron,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      });
      notifySuccess(`${preset.title} is on — ${preset.cadence.toLowerCase()}.`);
      onCreated();
    } catch (e) {
      notifyApiError(e, "Couldn't switch that on");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section aria-label="Preset automations" className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between gap-3">
        <p className="label-caps text-[var(--midnight-muted)]">Pick one to switch on</p>
        <p className="text-xs text-[var(--midnight-muted)]">
          Each becomes an ordinary automation you can edit below.
        </p>
      </div>
      <ul className="divide-y divide-[var(--edge)] overflow-hidden rounded-md border border-[var(--edge)] bg-card">
        {remaining.map((preset) => {
          const missing = preset.requires.filter((r) => !connected.has(r));
          const needsSetup = missing.length > 0;
          return (
            <li key={preset.key} className="flex items-center gap-4 px-4 py-3">
              <div className="min-w-0 flex-1">
                <p className="text-ui text-[var(--midnight)]">{preset.title}</p>
                <p className="mt-0.5 text-xs text-[var(--midnight-muted)]">{preset.summary}</p>
              </div>
              <p className="hidden shrink-0 text-xs text-[var(--midnight-muted)] sm:block">
                {preset.cadence}
              </p>
              {needsSetup ? (
                // The reason and the fix, not just the label. `Needs setup`
                // alone tells an owner something is wrong without telling
                // them what; naming the provider and linking to Integrations
                // is the difference between a dead switch and a next step.
                <a
                  href={INTEGRATIONS}
                  onClick={(e) => handleShellLinkClick(e, INTEGRATIONS)}
                  className="flex shrink-0 items-center gap-1 text-xs text-[var(--midnight-soft)] underline"
                  title={`Connect ${missing.join(", ")} in Integrations`}
                >
                  <Plug size={12} aria-hidden="true" />
                  Needs {missing.join(" + ")}
                </a>
              ) : null}
              <PresetSwitch
                label={preset.title}
                disabled={needsSetup || !assignee}
                busy={busy === preset.key}
                onSwitchOn={() => void switchOn(preset)}
              />
            </li>
          );
        })}
      </ul>
    </section>
  );
}
