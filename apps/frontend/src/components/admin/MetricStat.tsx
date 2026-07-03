const VALUE_TONE: Record<string, string> = {
  default: "text-zinc-100",
  error: "text-red-300",
  unknown: "text-zinc-300",
};

export function MetricStat({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  tone?: "default" | "error" | "unknown";
}) {
  return (
    <div>
      <div className={`text-2xl font-semibold tabular-nums ${VALUE_TONE[tone]}`}>{value}</div>
      <div className="text-xs text-zinc-500">{label}</div>
      {hint ? <div className="text-[10px] text-zinc-600">{hint}</div> : null}
    </div>
  );
}
