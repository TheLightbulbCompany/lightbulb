const VALUE_TONE: Record<string, string> = {
  default: "text-foreground",
  error: "text-err",
  unknown: "text-muted-foreground",
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
      <div className="text-xs text-muted-foreground">{label}</div>
      {hint ? <div className="text-2xs text-muted-foreground">{hint}</div> : null}
    </div>
  );
}
