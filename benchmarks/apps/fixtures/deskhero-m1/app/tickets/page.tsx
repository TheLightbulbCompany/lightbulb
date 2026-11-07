import Link from "next/link";
import { requireUser } from "@/lib/auth";
import { listTickets } from "@/lib/tickets";

export default async function Tickets() {
  const user = await requireUser();
  const rows = await listTickets(user.id);
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Your tickets</h1>
        <Link href="/tickets/new" data-testid="new-ticket-link" className="rounded bg-slate-900 px-3 py-1.5 text-white">New ticket</Link>
      </div>
      {rows.length === 0 ? (
        <p data-testid="ticket-empty" className="text-slate-500">No tickets yet.</p>
      ) : (
        <ul data-testid="ticket-list" className="divide-y rounded border bg-white">
          {rows.map((t) => (
            <li key={t.id} data-testid="ticket-row" className="flex items-center justify-between px-4 py-3">
              <Link href={`/tickets/${t.id}`} className="underline">{t.subject}</Link>
              <span className="text-sm text-slate-500">{t.priority} · {t.status}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
