import { eq } from "drizzle-orm";
import { notFound } from "next/navigation";
import { CancelBooking } from "@/components/cancel-booking";
import { practitioners, services } from "@/db/schema";
import { requireUser } from "@/lib/auth";
import { db } from "@/lib/db";
import { getBooking } from "@/lib/data";
import { fmtClinic } from "@/lib/time";

export default async function BookingPage({ params }: { params: Promise<{ id: string }> }) {
  const user = await requireUser();
  const b = await getBooking(user.id, (await params).id);
  if (!b) notFound();
  const [[p], [s]] = await Promise.all([db.select().from(practitioners).where(eq(practitioners.id, b.practitionerId)), db.select().from(services).where(eq(services.id, b.serviceId))]);
  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold">{s?.name} with {p?.name}</h1>
      <p>Starts: {fmtClinic(b.startAt)}</p>
      <p>Ends: <span data-testid="booking-detail-end" data-end={b.endAt.toISOString()}>{fmtClinic(b.endAt)}</span></p>
      <p>Status: <span data-testid="booking-detail-status">{b.status}</span></p>
      {b.status === "booked" && <CancelBooking id={b.id} />}
    </div>
  );
}
