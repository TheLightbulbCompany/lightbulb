export function MetricStat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div>
      <div className="text-2xl font-semibold tabular-nums text-zinc-100">{value}</div>
      <div className="text-xs text-zinc-500">{label}</div>
      {hint ? <div className="text-[10px] text-zinc-600">{hint}</div> : null}
    </div>
  );
}
