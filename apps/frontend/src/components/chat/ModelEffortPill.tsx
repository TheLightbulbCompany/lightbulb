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
          className="gap-1 h-8 px-2.5 rounded-full text-xs font-normal text-[#5a5549] hover:bg-[var(--cream-deeper)] hover:text-[var(--ink)]"
        >
          <span className="truncate max-w-40">{pillLabel}</span>
          <ChevronDown className="h-3 w-3 text-[#b5ae9e]" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="top"
        sideOffset={8}
        className="!w-[300px] !p-0 rounded-xl border-[var(--edge)] bg-white text-[var(--ink)] shadow-[0_2px_8px_rgba(26,26,26,0.04),0_12px_32px_rgba(26,26,26,0.10)]"
        style={{ maxHeight: 360 }}
      >
        {/* Model section */}
        <div className="p-2 border-b border-[#f0ece3]">
          <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-[#b5ae9e]">Model</div>
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
                      ? "bg-[var(--cream-deeper)] text-[var(--ink)] font-medium"
                      : "text-[#5a5549] hover:bg-[#f7f3ea] hover:text-[var(--ink)]",
                    (isStreaming || denied) && "opacity-40 cursor-not-allowed",
                  )}
                >
                  <span className="truncate text-left">{m.name ?? m.id}</span>
                  <span className="ml-2 flex shrink-0 items-center gap-1.5">
                    {isSel && <Check className="h-3.5 w-3.5 text-[var(--forest)]" />}
                    <span className="text-[10px] text-[#b5ae9e]">
                      {providerLabelFor(m)}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
          {isStreaming && (
            <div className="px-2 pt-1 text-[10px] text-[#b5ae9e]">
              Stop the run to switch model.
            </div>
          )}
        </div>

        {/* Reasoning section — only for reasoning models */}
        {showEffort && (
          <div className="p-2 border-b border-[#f0ece3]">
            <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-[#b5ae9e]">
              Reasoning
            </div>
            <div className="mx-1 flex gap-0.5 rounded-lg bg-[var(--cream-deeper)] p-0.5">
              {REASONING_LEVELS.map((lvl) => (
                <button
                  key={lvl}
                  type="button"
                  onClick={() => pickEffort(lvl)}
                  className={cn(
                    "flex-1 py-1 rounded-md text-xs capitalize transition-colors",
                    thinkingLevel === lvl
                      ? "bg-white text-[var(--ink)] shadow-sm"
                      : "text-[var(--mute)] hover:text-[#5a5549]",
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
            <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-[#b5ae9e]">Speed</div>
            <button
              type="button"
              onClick={() => toggleFast(!fastMode)}
              className="w-full flex items-center justify-between px-2 py-1.5 rounded-lg text-sm text-[#5a5549] hover:bg-[#f7f3ea]"
            >
              <span>Fast mode</span>
              <span
                className={cn(
                  "text-[10px] px-1.5 py-0.5 rounded-full",
                  fastMode
                    ? "bg-[var(--forest)]/10 text-[var(--forest)]"
                    : "bg-[var(--cream-deeper)] text-[#b5ae9e]",
                )}
              >
                {fastMode ? "On" : "Off"}
              </span>
            </button>
          </div>
        )}

        {error && (
          <div className="p-2 border-t border-[#f0ece3] text-[11px] text-[var(--danger)]">{error}</div>
        )}
      </PopoverContent>
    </Popover>
  );
}
