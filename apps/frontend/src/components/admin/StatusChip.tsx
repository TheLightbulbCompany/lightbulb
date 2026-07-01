import { cn } from "@/lib/utils";

export type OpsStatus = "running" | "provisioning" | "stopped" | "error" | "unknown";

const CLASSES: Record<string, string> = {
  running: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  provisioning: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  stopped: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  error: "bg-red-500/15 text-red-300 border-red-500/30",
  unknown: "bg-zinc-500/15 text-zinc-300 border-zinc-500/30",
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
