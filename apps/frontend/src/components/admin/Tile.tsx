import { cn } from "@/lib/utils";

const TONE: Record<string, string> = {
  ok: "border-emerald-500/30",
  warn: "border-amber-500/30",
  error: "border-red-500/40",
  info: "border-sky-500/30",
  unknown: "border-zinc-700",
};

export function Tile({
  title,
  label,
  tone = "unknown",
  children,
}: {
  title: string;
  label?: string;
  tone?: "ok" | "warn" | "error" | "info" | "unknown";
  children?: React.ReactNode;
}) {
  return (
    <div className={cn("rounded-lg border bg-zinc-900/40 p-3", TONE[tone])}>
      <div className="text-xs uppercase tracking-wide text-zinc-500">{title}</div>
      {label ? <div className="mt-1 text-lg font-semibold text-zinc-100">{label}</div> : null}
      {children}
    </div>
  );
}
