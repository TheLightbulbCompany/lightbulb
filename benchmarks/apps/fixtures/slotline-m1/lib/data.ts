import { and, asc, eq } from "drizzle-orm";
import { bookings, practitioners, services } from "@/db/schema";
import { db } from "./db";
import { clinicToUtc } from "./time";

const text = (v: unknown) => String(v ?? "").trim();

export function parsePractitioner(b: any): { error: string } | { name: string; specialty: string } {
  const name = text(b?.name);
  return name ? { name, specialty: text(b?.specialty) } : { error: "Name is required" };
}
export function parseService(b: any): { error: string } | { name: string; durationMinutes: number } {
  const name = text(b?.name);
  if (!name) return { error: "Name is required" };
  const durationMinutes = Number(b?.durationMinutes);
  if (!Number.isInteger(durationMinutes) || durationMinutes < 1 || durationMinutes > 24 * 60) return { error: "Length must be a whole number of minutes" };
  return { name, durationMinutes };
}

/** Every booking query is scoped to the patient. */
export const listBookings = (userId: string) =>
  db.select().from(bookings).where(eq(bookings.userId, userId)).orderBy(asc(bookings.startAt));

export const getBooking = async (userId: string, id: string) =>
  (await db.select().from(bookings).where(and(eq(bookings.id, id), eq(bookings.userId, userId))))[0];

/**
 * Book for `userId`. The start is `startAt` (UTC ISO) or `startLocal` (clinic time); the end comes from the service length.
 * Anything else in the body (end time, status, patient) is ignored.
 */
export async function createBooking(userId: string, b: any): Promise<{ error: string; status: number } | { booking: typeof bookings.$inferSelect }> {
  const [p] = await db.select().from(practitioners).where(eq(practitioners.id, text(b?.practitionerId)));
  const [s] = await db.select().from(services).where(eq(services.id, text(b?.serviceId)));
  if (!p) return { error: "Choose a practitioner", status: 400 };
  if (!s) return { error: "Choose a service", status: 400 };
  const startAt = typeof b?.startLocal === "string" ? clinicToUtc(b.startLocal) : typeof b?.startAt === "string" && /Z$/.test(b.startAt) && !isNaN(Date.parse(b.startAt)) ? new Date(b.startAt) : null;
  if (!startAt) return { error: "Enter a valid start time", status: 400 };
  const [booking] = await db.insert(bookings).values({ userId, practitionerId: p.id, serviceId: s.id, startAt, endAt: new Date(startAt.getTime() + s.durationMinutes * 60_000) }).returning();
  return { booking };
}
