import { cn } from "@/lib/utils";

export type OpsStatus = "running" | "provisioning" | "stopped" | "error" | "unknown";

// Semantic tones from the design system: calm when healthy, loud only on
// incidents. A degraded/unreadable source renders neutral, never ok.
const TONE = {
  ok: "border-ok text-ok",
  info: "border-info text-info",
  warn: "border-warn text-warn",
  err: "border-err text-err",
  neutral: "border-border text-muted-foreground",
} as const;

/**
 * Single source of the status language across the admin surface. Every
 * status-shaped string (container states, upstream probes, background
 * tasks, marketplace listing states) maps here; unrecognized values fall
 * back to neutral.
 */
const CLASSES: Record<string, string> = {
  // running / healthy
  running: TONE.ok,
  ok: TONE.ok,
  published: TONE.ok,
  // provisioning / in-progress
  provisioning: TONE.info,
  starting: TONE.info,
  // wipe preview — flags what a dry run would remove
  would_delete: TONE.warn,
  // wipe guard kept the resource on purpose — neither a success nor a delete
  refused: TONE.info,
  // stopped / warn
  stopped: TONE.warn,
  scaled_to_zero: TONE.warn,
  cancelled: TONE.warn,
  degraded: TONE.warn,
  review: TONE.warn,
  // error / incident
  error: TONE.err,
  failed: TONE.err,
  unreachable: TONE.err,
  down: TONE.err,
  taken_down: TONE.err,
  // unknown / inert
  unknown: TONE.neutral,
  unconfigured: TONE.neutral,
  unregistered: TONE.neutral,
  draft: TONE.neutral,
  retired: TONE.neutral,
};

export function StatusChip({ status, label }: { status: string; label?: string }) {
  const cls = CLASSES[status] ?? CLASSES.unknown;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-sm border px-2 py-0.5 text-2xs font-semibold uppercase tracking-caps",
        cls,
      )}
    >
      {label ?? status}
    </span>
  );
}
