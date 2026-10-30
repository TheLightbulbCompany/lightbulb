import { and, eq, ne } from "drizzle-orm";
import { NextResponse } from "next/server";
import { orders } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { db } from "@/lib/db";

// Test-mode checkout: no payment provider, nobody is charged. It only marks the customer's own order paid.
export async function POST(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const id = (await params).id;
  const [row] = await db.update(orders).set({ paymentStatus: "paid" }).where(and(eq(orders.id, id), eq(orders.userId, user.id), ne(orders.status, "cancelled"))).returning();
  if (row) return NextResponse.json(row);
  const [own] = await db.select({ id: orders.id }).from(orders).where(and(eq(orders.id, id), eq(orders.userId, user.id)));
  return own ? NextResponse.json({ error: "A cancelled order cannot be paid" }, { status: 409 }) : NextResponse.json({ error: "Not found" }, { status: 404 });
}
