import { cn } from "@/lib/utils";

export type OpsStatus = "running" | "provisioning" | "stopped" | "error" | "unknown";

// Semantic tones from the design system: calm when healthy, loud only on
// incidents. A degraded/unreadable source renders zinc, never green.
const TONE = {
  emerald: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  sky: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  amber: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  red: "bg-red-500/15 text-red-300 border-red-500/30",
  zinc: "bg-zinc-500/15 text-zinc-300 border-zinc-500/30",
} as const;

/**
 * Single source of the status language across the admin surface. Every
 * status-shaped string (container states, upstream probes, background
 * tasks, marketplace listing states) maps here; unrecognized values fall
 * back to zinc.
 */
const CLASSES: Record<string, string> = {
  // running / healthy
  running: TONE.emerald,
  ok: TONE.emerald,
  published: TONE.emerald,
  // provisioning / in-progress
  provisioning: TONE.sky,
  starting: TONE.sky,
  // wipe preview — flags what a dry run would remove
  would_delete: TONE.amber,
  // stopped / warn
  stopped: TONE.amber,
  scaled_to_zero: TONE.amber,
  cancelled: TONE.amber,
  degraded: TONE.amber,
  review: TONE.amber,
  // error / incident
  error: TONE.red,
  failed: TONE.red,
  unreachable: TONE.red,
  down: TONE.red,
  taken_down: TONE.red,
  // unknown / inert
  unknown: TONE.zinc,
  unconfigured: TONE.zinc,
  unregistered: TONE.zinc,
  draft: TONE.zinc,
  retired: TONE.zinc,
};

export function StatusChip({ status, label }: { status: string; label?: string }) {
  const cls = CLASSES[status] ?? CLASSES.unknown;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        cls,
      )}
    >
      {label ?? status}
    </span>
  );
}
