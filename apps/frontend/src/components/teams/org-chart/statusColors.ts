// Ported from upstream Paperclip's OrgChart status palette
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
// Status -> Tailwind color class for the agent status dot.

// Idle: muted gray. Running: green pulse. Paused: amber. Error/terminated: red/gray.

export const STATUS_DOT_CLASS: Record<string, string> = {
  idle: "bg-zinc-400 dark:bg-zinc-500",
  running: "bg-emerald-500 animate-pulse",
  paused: "bg-amber-500",
  error: "bg-red-500",
  terminated: "bg-zinc-300 dark:bg-zinc-600",
};

const DEFAULT = "bg-zinc-400 dark:bg-zinc-500";

export function statusDotClass(status: string | null | undefined): string {
  if (!status) return DEFAULT;
  return STATUS_DOT_CLASS[status] ?? DEFAULT;
}
