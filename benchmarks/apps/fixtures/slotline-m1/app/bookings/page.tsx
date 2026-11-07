import Link from "next/link";
import { requireUser } from "@/lib/auth";
import { db } from "@/lib/db";
import { listBookings } from "@/lib/data";
import { practitioners, services } from "@/db/schema";
import { fmtClinic } from "@/lib/time";

export default async function Bookings() {
  const user = await requireUser();
  const rows = await listBookings(user.id);
  const [ps, ss] = await Promise.all([db.select().from(practitioners), db.select().from(services)]);
  const pName = new Map(ps.map((p) => [p.id, p.name]));
  const sName = new Map(ss.map((s) => [s.id, s.name]));
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Your bookings</h1>
        <Link href="/bookings/new" data-testid="booking-new-button" className="rounded bg-slate-900 px-3 py-1.5 text-white">Book an appointment</Link>
      </div>
      {rows.length === 0 ? (
        <p data-testid="bookings-empty" className="text-slate-500">No bookings yet.</p>
      ) : (
        <ul data-testid="bookings-list" className="divide-y rounded border bg-white">
          {rows.map((b) => (
            <li key={b.id} data-testid="booking-row" data-start={b.startAt.toISOString()} data-end={b.endAt.toISOString()} data-status={b.status} className="flex items-center justify-between px-4 py-3">
              <span>{fmtClinic(b.startAt)} · {sName.get(b.serviceId)} with {pName.get(b.practitionerId)}</span>
              <span className="flex items-center gap-3 text-sm text-slate-500">
                <span data-testid="booking-row-status">{b.status}</span>
                <Link href={`/bookings/${b.id}`} data-testid="booking-row-link" className="underline">Open</Link>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
