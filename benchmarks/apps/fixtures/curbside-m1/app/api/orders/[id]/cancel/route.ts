import { and, eq } from "drizzle-orm";
import { NextResponse } from "next/server";
import { orders } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { db } from "@/lib/db";

export async function POST(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const id = (await params).id;
  // One conditional update: only the customer's own order, only while it is placed.
  const [row] = await db.update(orders).set({ status: "cancelled" }).where(and(eq(orders.id, id), eq(orders.userId, user.id), eq(orders.status, "placed"))).returning();
  if (row) return NextResponse.json(row);
  const [own] = await db.select({ status: orders.status }).from(orders).where(and(eq(orders.id, id), eq(orders.userId, user.id)));
  return own ? NextResponse.json({ error: `A ${own.status} order cannot be cancelled` }, { status: 409 }) : NextResponse.json({ error: "Not found" }, { status: 404 });
}
