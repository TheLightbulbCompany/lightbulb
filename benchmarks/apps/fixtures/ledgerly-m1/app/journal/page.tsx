import Link from "next/link";
import { requireUser } from "@/lib/auth";
import { listEntries } from "@/lib/data";
import { fmtCents } from "@/lib/money";

export default async function Journal() {
  const user = await requireUser();
  const rows = await listEntries(user.id);
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Journal</h1>
        <Link href="/journal/new" data-testid="entry-new-button" className="rounded bg-slate-900 px-3 py-1.5 text-white">New entry</Link>
      </div>
      {rows.length === 0 ? (
        <p data-testid="journal-empty" className="text-slate-500">No entries yet.</p>
      ) : (
        <ul data-testid="journal-list" className="divide-y rounded border bg-white">
          {rows.map((e) => (
            <li key={e.id} data-testid="entry-row" data-entry-id={e.id} data-entry-date={e.date} className="flex items-center justify-between px-4 py-3">
              <span>{e.date} · <span data-testid="entry-row-memo">{e.memo}</span></span>
              <span className="flex items-center gap-3 text-sm text-slate-500">
                <span data-testid="entry-row-total">{fmtCents(e.totalDebitCents)}</span>
                <Link href={`/journal/${e.id}`} data-testid="entry-row-link" className="underline">Open</Link>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
