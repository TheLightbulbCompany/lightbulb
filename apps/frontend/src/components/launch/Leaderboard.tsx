"use client";

import * as React from "react";
import { fetchLeaderboard, formatMoney, formatPct, type LeaderboardRow } from "@/lib/launch";

export function Leaderboard() {
  const [rows, setRows] = React.useState<LeaderboardRow[] | null>(null);
  React.useEffect(() => {
    let alive = true;
    const load = () => fetchLeaderboard().then((b) => alive && setRows(b.rows)).catch(() => alive && setRows([]));
    load();
    const t = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  if (rows === null) return <p className="lc-fine">Loading the board…</p>;
  if (rows.length === 0) return <p className="lc-fine">No public entrants yet.</p>;
  return (
    <table className="lc-table">
      <thead className="ld-mono">
        <tr><th>#</th><th>Entrant</th><th className="num">Revenue lift</th><th className="num">Growth</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.rank}>
            <td>{r.rank}</td>
            <td>{r.display_name}</td>
            <td className="num">{formatMoney(r.delta_cents, r.currency)}</td>
            <td className="num">{formatPct(r.pct)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
