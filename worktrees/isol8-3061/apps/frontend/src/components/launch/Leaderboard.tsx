"use client";

import * as React from "react";
import { fetchLeaderboard, formatMoney, formatPct, type LeaderboardRow } from "@/lib/launch";

interface Board {
  competing: number;
  on_board: number;
  rows: LeaderboardRow[];
}

export function Leaderboard() {
  const [board, setBoard] = React.useState<Board | null>(null);
  React.useEffect(() => {
    let alive = true;
    const load = () =>
      fetchLeaderboard()
        .then((b) => alive && setBoard(b))
        .catch(() => alive && setBoard({ competing: 0, on_board: 0, rows: [] }));
    load();
    const t = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  if (board === null) return <p className="lc-fine">Loading the board…</p>;
  return (
    <div>
      <p className="lc-fine">{board.on_board} on the board · {board.competing} competing</p>
      {board.rows.length === 0 ? (
        <p className="lc-fine">No public entrants yet.</p>
      ) : (
        <table className="lc-table">
          <thead className="ld-mono">
            <tr><th>#</th><th>Entrant</th><th className="num">Revenue lift</th><th className="num">Growth</th></tr>
          </thead>
          <tbody>
            {board.rows.map((r) => (
              <tr key={r.rank}>
                <td>{r.rank}</td>
                <td>{r.display_name}</td>
                <td className="num">{formatMoney(r.delta_cents, r.currency)}</td>
                <td className="num">{formatPct(r.pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
