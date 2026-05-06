// Ported from upstream Paperclip's OrgChart status palette
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
// Status -> editorial color tokens for the agent status dot.
//
// Mapping uses the same editorial palette as T16's StatusIcon:
//   active / online / running -> --forest        (success / live)
//   thinking                  -> --warn          (attention amber)
//   paused / idle             -> --mute          (neutral idle)
//   error / failed / terminated -> --danger      (error red) for "error",
//                                  --mute for "terminated" (cleanly stopped)
//   covered                   -> --forest-soft   (covered by active dependency)
//   stalled                   -> --warn          (review stalled)

// CSS-variable color per status. Used for inline `backgroundColor` style.
export const STATUS_DOT_COLOR: Record<string, string> = {
  active: "var(--forest)",
  online: "var(--forest)",
  running: "var(--forest)",
  thinking: "var(--warn)",
  idle: "var(--mute)",
  paused: "var(--mute)",
  terminated: "var(--mute)",
  error: "var(--danger)",
  failed: "var(--danger)",
  covered: "var(--forest-soft)",
  stalled: "var(--warn)",
};

const DEFAULT_COLOR = "var(--mute)";

// Tailwind className per status — controls size/pulse animation only.
// Active states pulse to indicate live activity. Color is applied via
// inline style from STATUS_DOT_COLOR (editorial tokens are CSS vars).
export const STATUS_DOT_CLASS: Record<string, string> = {
  active: "animate-pulse",
  online: "animate-pulse",
  running: "animate-pulse",
  thinking: "animate-pulse",
  idle: "",
  paused: "",
  terminated: "",
  error: "",
  failed: "",
  covered: "",
  stalled: "",
};

const DEFAULT_CLASS = "";

export function statusDotClass(status: string | null | undefined): string {
  if (!status) return DEFAULT_CLASS;
  return STATUS_DOT_CLASS[status] ?? DEFAULT_CLASS;
}

export function statusDotColor(status: string | null | undefined): string {
  if (!status) return DEFAULT_COLOR;
  return STATUS_DOT_COLOR[status] ?? DEFAULT_COLOR;
}
