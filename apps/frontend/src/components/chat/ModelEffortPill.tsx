"use client";

import * as React from "react";
import { Check, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { useAgentModels } from "@/hooks/useAgentModels";
import {
  useSessionModelControls,
  LiveSessionModelSwitchError,
} from "@/hooks/useSessionModelControls";
import { isReasoningModel, providerLabelFor, type ModelEntry } from "@/lib/models";

const REASONING_LEVELS = ["off", "low", "medium", "high"] as const;

export interface ModelEffortPillProps {
  agentId: string | null;
  activeSessionId: string | null;
  isStreaming: boolean;
  /** Current session model ref, `${provider}/${id}`. */
  selectedModelRef: string;
  thinkingLevel: string;
  fastMode: boolean;
  providerChoice: string | null;
  byoProvider: string | null;
  onChanged?: () => void;
}

function refOf(entry: ModelEntry): string {
  return `${entry.provider}/${entry.id}`;
}

export function ModelEffortPill({
  agentId,
  activeSessionId,
  isStreaming,
  selectedModelRef,
  thinkingLevel,
  fastMode,
  providerChoice,
  byoProvider,
  onChanged,
}: ModelEffortPillProps) {
  const [open, setOpen] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const { models, defaultModel } = useAgentModels(agentId);
  const { setModel, setThinkingLevel, setFastMode } = useSessionModelControls(
    agentId,
    activeSessionId,
    isStreaming,
  );

  const selected =
    models.find((m) => refOf(m) === selectedModelRef) ??
    models.find((m) => refOf(m) === defaultModel) ??
    models[0];

  const showEffort = selected ? isReasoningModel(selected) : false;
  const showSpeed = providerChoice === "byo_key" && byoProvider === "openai";
  // Only annotate the pill when a concrete reasoning level is set. An empty
  // thinkingLevel means "inherit the default" — show no suffix rather than a
  // dangling separator.
  const effortLabel =
    showEffort && thinkingLevel && thinkingLevel !== "off" ? ` · ${thinkingLevel}` : "";
  const pillLabel = `${selected?.name ?? "Model"}${effortLabel}`;

  const pick = async (entry: ModelEntry) => {
    if (isStreaming) return; // rows are disabled; guard the handler too
    setError(null);
    try {
      await setModel(entry);
      onChanged?.();
      setOpen(false);
    } catch (err) {
      setError(
        err instanceof LiveSessionModelSwitchError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to set model",
      );
    }
  };

  const pickEffort = async (level: string) => {
    setError(null);
    try {
      await setThinkingLevel(level);
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to set reasoning");
    }
  };

  const toggleFast = async (next: boolean) => {
    setError(null);
    try {
      await setFastMode(next);
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to set speed");
    }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          data-testid="model-effort-pill"
          className="gap-1 h-8 min-w-0 px-2.5 rounded-full text-xs font-normal text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
        >
          <span className="truncate max-w-40">{pillLabel}</span>
          <ChevronDown className="h-3 w-3 shrink-0 text-[var(--midnight-faint)]" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="top"
        sideOffset={8}
        className="!w-[300px] !p-0 rounded-xl border-[var(--edge)] bg-cream text-[var(--midnight)] shadow-[0_2px_8px_color-mix(in_srgb,var(--midnight)_4%,transparent),0_12px_32px_color-mix(in_srgb,var(--midnight)_10%,transparent)]"
        style={{ maxHeight: 360 }}
      >
        {/* Model section */}
        <div className="p-2 border-b border-[var(--edge-soft)]">
          <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-[var(--midnight-label)]">Model</div>
          <div className="max-h-48 overflow-y-auto">
            {models.map((m) => {
              const isSel = refOf(m) === selectedModelRef;
              const denied = m.available === false;
              return (
                <button
                  key={refOf(m)}
                  type="button"
                  disabled={isStreaming || denied}
                  title={denied ? "Model access denied" : m.id}
                  onClick={() => pick(m)}
                  className={cn(
                    "w-full flex items-center justify-between px-2 py-1.5 rounded-lg text-sm transition-colors",
                    isSel
                      ? "bg-[var(--cream-deeper)] text-[var(--midnight)] font-medium"
                      : "text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]",
                    (isStreaming || denied) && "opacity-40 cursor-not-allowed",
                  )}
                >
                  <span className="truncate text-left">{m.name ?? m.id}</span>
                  <span className="ml-2 flex shrink-0 items-center gap-1.5">
                    {isSel && <Check className="h-3.5 w-3.5 text-[var(--midnight)]" />}
                    <span className="text-[10px] text-[var(--midnight-muted)]">
                      {providerLabelFor(m)}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
          {isStreaming && (
            <div className="px-2 pt-1 text-[10px] text-[var(--midnight-muted)]">
              Stop the run to switch model.
            </div>
          )}
        </div>

        {/* Reasoning section — only for reasoning models */}
        {showEffort && (
          <div className="p-2 border-b border-[var(--edge-soft)]">
            <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-[var(--midnight-label)]">
              Reasoning
            </div>
            <div className="mx-1 flex gap-0.5 rounded-lg bg-[var(--cream-deep)] p-0.5">
              {REASONING_LEVELS.map((lvl) => (
                <button
                  key={lvl}
                  type="button"
                  onClick={() => pickEffort(lvl)}
                  className={cn(
                    "flex-1 py-1 rounded-md text-xs capitalize transition-colors",
                    thinkingLevel === lvl
                      ? "bg-cream text-[var(--midnight)] shadow-sm"
                      : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]",
                  )}
                >
                  {lvl}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Speed (fast-mode) — only BYO-OpenAI */}
        {showSpeed && (
          <div className="p-2">
            <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-[var(--midnight-label)]">Speed</div>
            <button
              type="button"
              onClick={() => toggleFast(!fastMode)}
              className="w-full flex items-center justify-between px-2 py-1.5 rounded-lg text-sm text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)]"
            >
              <span>Fast mode</span>
              <span
                className={cn(
                  "text-[10px] px-1.5 py-0.5 rounded-full",
                  fastMode
                    ? "bg-[var(--midnight)]/10 text-[var(--midnight)]"
                    : "bg-[var(--cream-deep)] text-[var(--midnight-muted)]",
                )}
              >
                {fastMode ? "On" : "Off"}
              </span>
            </button>
          </div>
        )}

        {error && (
          <div className="p-2 border-t border-[var(--edge-soft)] text-[11px] text-[var(--err)]">{error}</div>
        )}
      </PopoverContent>
    </Popover>
  );
}
