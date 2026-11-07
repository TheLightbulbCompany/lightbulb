import { asc } from "drizzle-orm";
import { BookingForm } from "@/components/booking-form";
import { practitioners, services } from "@/db/schema";
import { requireUser } from "@/lib/auth";
import { db } from "@/lib/db";

export default async function NewBooking() {
  await requireUser();
  const [ps, ss] = await Promise.all([db.select().from(practitioners).orderBy(asc(practitioners.name)), db.select().from(services).orderBy(asc(services.name))]);
  return <BookingForm practitioners={ps.map((p) => ({ id: p.id, name: p.name }))} services={ss.map((s) => ({ id: s.id, name: s.name }))} />;
}
