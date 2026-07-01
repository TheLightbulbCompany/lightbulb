"use client";

import * as React from "react";
import { ChevronDown } from "lucide-react";
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
  const effortLabel = showEffort && thinkingLevel !== "off" ? ` · ${thinkingLevel}` : "";
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
          variant="outline"
          size="sm"
          data-testid="model-effort-pill"
          className="gap-1.5 h-7 px-2.5 rounded-full bg-white border-[#e0dbd0] text-[#5a5549] hover:bg-[#f3efe6] text-xs"
        >
          <span className="truncate max-w-40">{pillLabel}</span>
          <ChevronDown className="h-3 w-3 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="top"
        sideOffset={8}
        className="!w-[300px] !p-0 border-[#333] text-white shadow-2xl"
        style={{ background: "#111111", maxHeight: 360 }}
      >
        {/* Model section */}
        <div className="p-2 border-b border-white/10">
          <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-white/40">Model</div>
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
                    "w-full flex items-center justify-between px-2 py-1.5 rounded text-sm transition-colors",
                    isSel ? "bg-white/10 text-white" : "text-white/70 hover:bg-white/5",
                    (isStreaming || denied) && "opacity-40 cursor-not-allowed",
                  )}
                >
                  <span className="truncate text-left">{m.name ?? m.id}</span>
                  <span className="ml-2 shrink-0 text-[10px] text-white/30">
                    {providerLabelFor(m)}
                  </span>
                </button>
              );
            })}
          </div>
          {isStreaming && (
            <div className="px-2 pt-1 text-[10px] text-white/30">
              Stop the run to switch model.
            </div>
          )}
        </div>

        {/* Reasoning section — only for reasoning models */}
        {showEffort && (
          <div className="p-2 border-b border-white/10">
            <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-white/40">
              Reasoning
            </div>
            <div className="flex gap-1 px-1">
              {REASONING_LEVELS.map((lvl) => (
                <button
                  key={lvl}
                  type="button"
                  onClick={() => pickEffort(lvl)}
                  className={cn(
                    "flex-1 py-1 rounded text-xs capitalize transition-colors",
                    thinkingLevel === lvl
                      ? "bg-white/15 text-white"
                      : "text-white/60 hover:bg-white/5",
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
            <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-white/40">Speed</div>
            <button
              type="button"
              onClick={() => toggleFast(!fastMode)}
              className="w-full flex items-center justify-between px-2 py-1.5 rounded text-sm text-white/70 hover:bg-white/5"
            >
              <span>Fast mode</span>
              <span
                className={cn(
                  "text-[10px] px-1.5 py-0.5 rounded-full",
                  fastMode
                    ? "bg-emerald-500/15 text-emerald-400"
                    : "bg-white/5 text-white/40",
                )}
              >
                {fastMode ? "On" : "Off"}
              </span>
            </button>
          </div>
        )}

        {error && (
          <div className="p-2 border-t border-white/10 text-[11px] text-red-400">{error}</div>
        )}
      </PopoverContent>
    </Popover>
  );
}
