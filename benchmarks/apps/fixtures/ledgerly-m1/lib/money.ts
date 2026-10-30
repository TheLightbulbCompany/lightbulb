/** "1250.10" -> 125010, exactly (string arithmetic, no floats); null when it is not dollars with at most two decimals. */
export function dollarsToCents(s: string): number | null {
  const m = /^(\d{1,12})(?:\.(\d{1,2}))?$/.exec(s.trim());
  return m ? Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0")) : null;
}
export const fmtCents = (c: number) => `$${Math.trunc(c / 100).toLocaleString("en-US")}.${String(c % 100).padStart(2, "0")}`;
