import { and, eq } from "drizzle-orm";
import { NextResponse } from "next/server";
import { bookings } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { getBooking } from "@/lib/data";
import { db } from "@/lib/db";

type Ctx = { params: Promise<{ id: string }> };
const no = (status: number, error: string) => NextResponse.json({ error }, { status });

export async function GET(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const b = await getBooking(user.id, (await params).id);
  return b ? NextResponse.json(b) : no(404, "Not found");
}

// The only change a booking allows is { status: "cancelled" }; it keeps the row.
export async function PATCH(req: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const b = await req.json().catch(() => ({}));
  if (b?.status !== "cancelled") return no(400, "Only cancelling is supported");
  const [row] = await db.update(bookings).set({ status: "cancelled" }).where(and(eq(bookings.id, (await params).id), eq(bookings.userId, user.id))).returning();
  return row ? NextResponse.json(row) : no(404, "Not found");
}
