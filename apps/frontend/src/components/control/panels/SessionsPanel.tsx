"use client";

import { useState, useCallback } from "react";
import { Loader2, RefreshCw, Trash2 } from "lucide-react";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/* ── Types ─────────────────────────────────────────────── */

interface Session {
  key: string;
  kind?: "direct" | "group" | "global" | "unknown";
  agentId?: string;
  model?: string;
  modelProvider?: string;
  label?: string;
  displayName?: string;
  surface?: string;
  subject?: string;
  room?: string;
  updatedAt?: number | null;
  thinkingLevel?: string;
  verboseLevel?: string;
  reasoningLevel?: string;
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  contextTokens?: number;
  [key: string]: unknown;
}

interface SessionsResponse {
  ts?: number;
  path?: string;
  count?: number;
  defaults?: { model?: string; contextTokens?: number };
  sessions?: Session[];
}

/* ── Constants ─────────────────────────────────────────── */

const THINK_LEVELS = ["", "off", "minimal", "low", "medium", "high", "xhigh"];
const BINARY_THINK_LEVELS = ["", "off", "on"];
const VERBOSE_LEVELS = [
  { value: "", label: "inherit" },
  { value: "off", label: "off (explicit)" },
  { value: "on", label: "on" },
  { value: "full", label: "full" },
];
const REASONING_LEVELS = ["", "off", "on", "stream"];

/* ── Helpers ───────────────────────────────────────────── */

function isBinaryThinkingProvider(provider?: string | null): boolean {
  if (!provider) return false;
  const n = provider.trim().toLowerCase();
  return n === "z.ai" || n === "z-ai";
}

function resolveThinkOptions(provider?: string | null): string[] {
  const base = isBinaryThinkingProvider(provider) ? BINARY_THINK_LEVELS : THINK_LEVELS;
  return [...base];
}

function withCurrentOption(options: string[], current: string): string[] {
  if (!current || options.includes(current)) return options;
  return [...options, current];
}

function withCurrentLabeledOption(
  options: { value: string; label: string }[],
  current: string,
): { value: string; label: string }[] {
  if (!current || options.some((o) => o.value === current)) return options;
  return [...options, { value: current, label: `${current} (custom)` }];
}

function resolveThinkDisplay(value: string, isBinary: boolean): string {
  if (!isBinary) return value;
  if (!value || value === "off") return value;
  return "on";
}

function resolveThinkPatchValue(value: string, isBinary: boolean): string | null {
  if (!value) return null;
  if (!isBinary) return value;
  return value === "on" ? "low" : value;
}

function formatRelativeTime(ms: number | null | undefined): string {
  if (ms == null) return "n/a";
  const diff = Date.now() - ms;
  if (diff < 0) return "just now";
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function formatTokens(row: Session): string {
  if (row.totalTokens == null) return "n/a";
  const total = row.totalTokens ?? 0;
  const ctx = row.contextTokens ?? 0;
  return ctx ? `${total} / ${ctx}` : String(total);
}

/* ── Component ─────────────────────────────────────────── */

export function SessionsPanel() {
  // Filter state
  const [activeMinutes, setActiveMinutes] = useState("");
  const [limit, setLimit] = useState("");
  const [includeGlobal, setIncludeGlobal] = useState(true);
  const [includeUnknown, setIncludeUnknown] = useState(true);

  // Build RPC params from filter state
  const params: Record<string, unknown> = {
    includeGlobal,
    includeUnknown,
    includeDerivedTitles: true,
    includeLastMessage: true,
  };
  const am = parseInt(activeMinutes, 10);
  if (am > 0) params.activeMinutes = am;
  const lim = parseInt(limit, 10);
  if (lim > 0) params.limit = lim;

  const { data: rawData, error, isLoading, mutate } = useGatewayRpc<SessionsResponse | Session[]>(
    "sessions.list",
    params,
  );
  const callRpc = useGatewayRpcMutation();

  const sessions: Session[] = Array.isArray(rawData)
    ? rawData
    : (rawData as SessionsResponse)?.sessions ?? [];

  const storePath = !Array.isArray(rawData) ? (rawData as SessionsResponse)?.path : undefined;

  /* ── Handlers ──────────────────────────────────────── */

  const handlePatch = useCallback(
    async (
      key: string,
      patch: {
        label?: string | null;
        thinkingLevel?: string | null;
        verboseLevel?: string | null;
        reasoningLevel?: string | null;
      },
    ) => {
      try {
        await callRpc("sessions.patch", { key, ...patch });
        // Unawaited revalidation: a rejection here must not become an
        // unhandled promise rejection (the patch already persisted).
        void mutate().catch(() => {});
      } catch (err) {
        console.error("Failed to patch session:", err);
      }
    },
    [callRpc, mutate],
  );

  const handleDelete = useCallback(
    async (key: string) => {
      const confirmed = window.confirm(
        `Delete session "${key}"?\n\nDeletes the session entry and archives its transcript.`,
      );
      if (!confirmed) return;
      try {
        await callRpc("sessions.delete", { key, deleteTranscript: true });
        void mutate().catch(() => {});
      } catch (err) {
        console.error("Failed to delete session:", err);
      }
    },
    [callRpc, mutate],
  );

  /* ── Loading state ─────────────────────────────────── */

  if (isLoading && !rawData) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-[var(--mute)]" />
      </div>
    );
  }

  /* ── Render ────────────────────────────────────────── */

  return (
    <div className="p-6 space-y-4 overflow-auto bg-[var(--cream)]">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-[var(--ink)]">Sessions</h2>
          <p className="text-xs text-[var(--mute)]">
            Active session keys and per-session overrides.
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => mutate()}
          disabled={isLoading}
        >
          {isLoading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
        </Button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <label className="text-[11px] text-[var(--mute)]">Active within (min)</label>
          <Input
            type="number"
            className="h-8 w-28 text-xs"
            placeholder="0"
            value={activeMinutes}
            onChange={(e) => setActiveMinutes(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[11px] text-[var(--mute)]">Limit</label>
          <Input
            type="number"
            className="h-8 w-28 text-xs"
            placeholder="0"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
        </div>
        <label className="flex items-center gap-1.5 text-xs text-[var(--mute)] cursor-pointer pb-1">
          <input
            type="checkbox"
            checked={includeGlobal}
            onChange={(e) => setIncludeGlobal(e.target.checked)}
            className="accent-[var(--forest)]"
          />
          Include global
        </label>
        <label className="flex items-center gap-1.5 text-xs text-[var(--mute)] cursor-pointer pb-1">
          <input
            type="checkbox"
            checked={includeUnknown}
            onChange={(e) => setIncludeUnknown(e.target.checked)}
            className="accent-[var(--forest)]"
          />
          Include unknown
        </label>
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-700">
          {error.message}
        </div>
      )}

      {/* Store path */}
      {storePath && (
        <p className="text-[11px] text-[var(--mute)]/50">Store: {storePath}</p>
      )}

      {/* Table */}
      {sessions.length === 0 ? (
        <p className="text-sm text-[var(--mute)]">No sessions found.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="border-b border-[var(--edge)] text-left text-[var(--mute)]">
                <th className="py-2 pr-3 font-medium">Key</th>
                <th className="py-2 pr-3 font-medium">Label</th>
                <th className="py-2 pr-3 font-medium">Kind</th>
                <th className="py-2 pr-3 font-medium">Updated</th>
                <th className="py-2 pr-3 font-medium">Tokens</th>
                <th className="py-2 pr-3 font-medium">Thinking</th>
                <th className="py-2 pr-3 font-medium">Verbose</th>
                <th className="py-2 pr-3 font-medium">Reasoning</th>
                <th className="py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <SessionRow
                  key={s.key}
                  session={s}
                  disabled={isLoading}
                  onPatch={handlePatch}
                  onDelete={handleDelete}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ── Session Row ─────────────────────────────────────── */

function SessionRow({
  session: s,
  disabled,
  onPatch,
  onDelete,
}: {
  session: Session;
  disabled: boolean;
  onPatch: (key: string, patch: Record<string, string | null>) => void;
  onDelete: (key: string) => void;
}) {
  const isBinary = isBinaryThinkingProvider(s.modelProvider);
  const rawThinking = s.thinkingLevel ?? "";
  const thinking = resolveThinkDisplay(rawThinking, isBinary);
  const thinkOptions = withCurrentOption(resolveThinkOptions(s.modelProvider), thinking);

  const verbose = s.verboseLevel ?? "";
  const verboseOptions = withCurrentLabeledOption([...VERBOSE_LEVELS], verbose);

  const reasoning = s.reasoningLevel ?? "";
  const reasoningOptions = withCurrentOption([...REASONING_LEVELS], reasoning);

  const displayName =
    s.displayName && s.displayName.trim() !== s.key && s.displayName.trim() !== (s.label ?? "").trim()
      ? s.displayName.trim()
      : null;

  return (
    <tr className="border-b border-[var(--edge)]/50 hover:bg-[var(--cream-deeper)]/50 transition-colors">
      {/* Key */}
      <td className="py-2 pr-3 font-mono text-[11px] max-w-[180px]">
        <div className="truncate">{s.key}</div>
        {displayName && (
          <div className="text-[10px] text-[var(--mute)]/50 truncate">{displayName}</div>
        )}
      </td>

      {/* Label (editable) */}
      <td className="py-2 pr-3">
        <input
          className="bg-transparent border-b border-transparent hover:border-[var(--edge)] focus:border-[var(--forest)] outline-none text-xs w-24 py-0.5 transition-colors text-[var(--ink)]"
          defaultValue={s.label ?? ""}
          placeholder="(optional)"
          disabled={disabled}
          onBlur={(e) => {
            const val = e.target.value.trim();
            const prev = (s.label ?? "").trim();
            if (val !== prev) {
              onPatch(s.key, { label: val || null });
            }
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
      </td>

      {/* Kind */}
      <td className="py-2 pr-3">
        <span
          className={cn(
            "inline-block px-1.5 py-0.5 rounded text-[10px] font-medium",
            s.kind === "direct" && "bg-[#e8f5e9] text-[#2d8a4e]",
            s.kind === "group" && "bg-blue-50 text-[var(--forest)]",
            s.kind === "global" && "bg-amber-50 text-amber-700",
            (!s.kind || s.kind === "unknown") && "bg-[var(--cream-deeper)] text-[var(--mute)]",
          )}
        >
          {s.kind || "unknown"}
        </span>
      </td>

      {/* Updated */}
      <td className="py-2 pr-3 text-[var(--mute)] whitespace-nowrap">
        {formatRelativeTime(s.updatedAt)}
      </td>

      {/* Tokens */}
      <td className="py-2 pr-3 font-mono text-[var(--mute)] whitespace-nowrap">
        {formatTokens(s)}
      </td>

      {/* Thinking */}
      <td className="py-2 pr-3">
        <select
          className="bg-white border border-[var(--edge)] rounded px-1 py-0.5 text-[11px] outline-none focus:border-[var(--forest)] cursor-pointer text-[var(--ink)]"
          value={thinking}
          disabled={disabled}
          onChange={(e) => {
            const val = e.target.value;
            onPatch(s.key, {
              thinkingLevel: resolveThinkPatchValue(val, isBinary),
            });
          }}
        >
          {thinkOptions.map((level) => (
            <option key={level} value={level}>
              {level || "inherit"}
            </option>
          ))}
        </select>
      </td>

      {/* Verbose */}
      <td className="py-2 pr-3">
        <select
          className="bg-white border border-[var(--edge)] rounded px-1 py-0.5 text-[11px] outline-none focus:border-[var(--forest)] cursor-pointer text-[var(--ink)]"
          value={verbose}
          disabled={disabled}
          onChange={(e) => {
            onPatch(s.key, { verboseLevel: e.target.value || null });
          }}
        >
          {verboseOptions.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </td>

      {/* Reasoning */}
      <td className="py-2 pr-3">
        <select
          className="bg-white border border-[var(--edge)] rounded px-1 py-0.5 text-[11px] outline-none focus:border-[var(--forest)] cursor-pointer text-[var(--ink)]"
          value={reasoning}
          disabled={disabled}
          onChange={(e) => {
            onPatch(s.key, { reasoningLevel: e.target.value || null });
          }}
        >
          {reasoningOptions.map((level) => (
            <option key={level} value={level}>
              {level || "inherit"}
            </option>
          ))}
        </select>
      </td>

      {/* Delete */}
      <td className="py-2">
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-[var(--mute)] hover:text-red-600"
          disabled={disabled}
          onClick={() => onDelete(s.key)}
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      </td>
    </tr>
  );
}
